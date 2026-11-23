"""``python -m apiserver.contract --workspace DIR [--server DIR] [--allow FILE]``

DIR holds the checkouts ``cairnml`` (its ``cairnml_agent`` folder is the agent) and ``cairnml-web``. Exits 1 when a problem is not on the allow-list
or an allow-list entry no longer matches a problem."""

import argparse
import sys
from pathlib import Path

from apiserver.contract.check import check_workspace

DEFAULT_SERVER = Path(__file__).parents[2]
DEFAULT_ALLOW = Path(__file__).parent / "allow.txt"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apiserver.contract", description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--server", type=Path, default=DEFAULT_SERVER)
    parser.add_argument("--allow", type=Path, default=DEFAULT_ALLOW)
    args = parser.parse_args(argv)
    for name in ("cairnml", "cairnml-web"):
        if not (args.workspace / name).is_dir():
            print(f"error: {args.workspace / name} is missing", file=sys.stderr)
            return 2
    try:
        unallowed, stale, allowed_count = check_workspace(args.workspace, args.server, args.allow)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for problem in unallowed:
        print(f"PROBLEM {problem}")
    for key in stale:
        print(f"STALE allow-list entry (no longer a problem): {key}")
    print(f"{len(unallowed)} problem(s), {len(stale)} stale allow-list entr(ies), {allowed_count} allowed")
    return 1 if unallowed or stale else 0


if __name__ == "__main__":
    sys.exit(main())
