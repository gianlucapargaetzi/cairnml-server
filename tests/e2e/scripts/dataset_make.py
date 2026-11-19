"""Builds a dataset in a new git repository, connects it to a GitHub repository and publishes it twice: three images
and a CSV, then a second version in which one image is gone and another file is new. Prints the commits and the digest
of each file in each version, so another machine can check what it gets back (see dataset_fetch.py). With --unpushed,
a third commit stays local.

The commits go to the branch given by --branch, never to the default branch: the dataset is a new history on that
branch, so the repository's own history is untouched. The caller removes the branch afterwards (dataset_branch_delete.py).
The files go to the server's dataset bucket, not to GitHub."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import deterministic_bytes, file_digest, report  # noqa: E402

from cairnml.datasets import DatasetRepo, SampleWriter  # noqa: E402


BRANCH_PREFIX = "e2e/"


def digests(data: Path) -> dict:
    return {p.relative_to(data).as_posix(): file_digest(p) for p in sorted(data.rglob("*")) if p.is_file()}


def describe(data: Path) -> None:
    """Write the standard sample table for the images now in data/ (a native dataset)."""
    names = sorted(p.relative_to(data).as_posix() for p in (data / "images").glob("*.bin"))
    writer = SampleWriter(data, {"image": "file"}, {}, source_root=data)
    for name in names:
        writer.add(name, "train", {"image": name}, {})
    writer.close()


def main() -> None:
    # The runner has no git identity of its own, and the commits below need one.
    for name, value in (("NAME", "E2E Test"), ("EMAIL", "e2e@example.test")):
        os.environ.setdefault(f"GIT_AUTHOR_{name}", value)
        os.environ.setdefault(f"GIT_COMMITTER_{name}", value)
    parser = argparse.ArgumentParser(description="create a dataset repository and publish two versions")
    parser.add_argument(
        "--url", required=True, help="the GitHub repository to publish to, as https://github.com/owner/name.git"
    )
    parser.add_argument(
        "--credential", required=True, help="the id of the GitHub App credential that reads and writes it"
    )
    parser.add_argument("--branch", required=True, help="the branch to publish to: it must start with e2e/")
    parser.add_argument("--work", required=True, help="the folder of the new local repository")
    parser.add_argument("--unpushed", action="store_true")
    args = parser.parse_args()
    if not args.branch.startswith(BRANCH_PREFIX):
        raise SystemExit(f"refusing to publish to {args.branch}: the test branches all start with {BRANCH_PREFIX}")

    work = Path(args.work)
    work.mkdir(parents=True)
    # A branch of its own: on the default branch the tool would publish through a pull request instead.
    subprocess.run(["git", "init", "-q", "-b", args.branch], cwd=work, check=True, capture_output=True)
    repo = DatasetRepo(work)
    repo.init()
    repo.remote_add(args.url, credential=args.credential)  # origin, the dataset's storage (s3), the server's record
    if repo.warnings:
        raise SystemExit("the repository is not readable with the credential:\n" + "\n".join(repo.warnings))
    data = work / "data"

    (data / "images").mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (data / "images" / f"sample-{index}.bin").write_bytes(deterministic_bytes(50_000 + index, seed=index))
    (data / "labels.csv").write_text("id,label\n0,cat\n1,dog\n2,cat\n")
    describe(data)
    repo.add()
    first = repo.commit("three images and their labels")
    repo.push()
    first_files = digests(data)

    (data / "images" / "sample-2.bin").unlink()
    (data / "images" / "sample-3.bin").write_bytes(deterministic_bytes(50_003, seed=3))
    describe(data)
    repo.add()
    second = repo.commit("replace sample 2 with sample 3")
    repo.push()
    second_files = digests(data)

    third = None
    if args.unpushed:
        (data / "late.txt").write_text("not pushed")
        describe(data)
        repo.add()
        third = repo.commit("not pushed")
    report(
        {
            "first": first,
            "second": second,
            "third": third,
            "first_files": first_files,
            "second_files": second_files,
            "branch": args.branch,
        }
    )


if __name__ == "__main__":
    main()
