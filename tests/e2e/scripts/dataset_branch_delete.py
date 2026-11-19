"""Removes the branches dataset_make.py published to, from the GitHub repository, and says what is left under the
prefix. The write token is the one the server gives the signed-in owner of the dataset (the same one the SDK pushes
with); no secret is read from anywhere else. Only branches under e2e/ are ever touched, one run's prefix at a time."""

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import report  # noqa: E402

from cairnml.datasets.credentials import fetch_person_git_token  # noqa: E402
from cairnml.datasets.gitutil import Git  # noqa: E402


def branches_under(git: Git, prefix: str) -> list:
    out = git.run("ls-remote", "--heads", "origin", f"refs/heads/{prefix}*")
    return sorted(line.split("refs/heads/", 1)[1] for line in out.splitlines() if "refs/heads/" in line)


def main() -> None:
    parser = argparse.ArgumentParser(description="delete the test branches of one run")
    parser.add_argument("--url", required=True, help="https://github.com/owner/name.git")
    parser.add_argument("--prefix", required=True, help="the run's branch prefix, e2e/<run id>/")
    args = parser.parse_args()
    if not args.prefix.startswith("e2e/") or not args.prefix.endswith("/") or args.prefix == "e2e/":
        raise SystemExit(f"refusing to delete under {args.prefix!r}: it must be e2e/<run id>/")

    token = fetch_person_git_token(args.url, "read_write")
    with tempfile.TemporaryDirectory() as folder:
        git = Git.init(folder)
        git.run("remote", "add", "origin", args.url)
        git = Git(folder, token=token)
        found = branches_under(git, args.prefix)
        for branch in found:
            git.run("push", "-q", "origin", "--delete", f"refs/heads/{branch}")
        report({"deleted": found, "left": branches_under(git, args.prefix)})


if __name__ == "__main__":
    main()
