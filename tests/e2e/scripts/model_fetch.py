"""Gets a registered model by id and prints the digest of its weights file. Run on another machine by the tests."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import file_digest, report  # noqa: E402

from cairnml import Model  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="get a model and check its weights")
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args()

    local = Path(Model(model_id=args.model_id).get_local_copy())
    report({"digest": file_digest(local), "name": local.name})


if __name__ == "__main__":
    main()
