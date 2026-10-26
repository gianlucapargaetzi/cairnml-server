"""Trains nothing: writes a weights file and registers it as the output model of a run (OutputModel)."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import deterministic_bytes, file_digest, report  # noqa: E402

from cairnml import OutputModel, Task  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="register a model file under a run")
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--work", required=True)
    args = parser.parse_args()

    weights = Path(args.work) / "weights.pt"
    weights.parent.mkdir(parents=True, exist_ok=True)
    weights.write_bytes(deterministic_bytes(120_000, seed=7))
    task = Task.init(project_name=args.project, task_name=args.name, reuse_last_task_id=False)
    model = OutputModel(task=task, framework="PyTorch", name=f"{args.name}-model")
    model.update_weights(weights_filename=str(weights), auto_delete_file=False, iteration=1)
    report({"task_id": task.id, "model_id": model.id, "digest": file_digest(weights)})
    task.close()


if __name__ == "__main__":
    main()
