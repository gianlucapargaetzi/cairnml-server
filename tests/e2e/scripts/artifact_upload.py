"""Uploads one artifact of every kind in one task, the way a training script does (README section 2 of docs/e2e_testing/E2E.md).

Kinds: a file, a folder (zipped by the SDK), a dict (JSON), a DataFrame, a numpy array, a PIL image, a pickled
object, a file with metadata, and optionally a large file (--big-mb) that goes up in parts. The digests of the
content are written to --expect, so the check script can compare what comes back with what was sent.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from common import array_digest, deterministic_bytes, file_digest, folder_digest, frame_digest  # noqa: E402
from common import image_digest, json_digest, pickled_digest, report  # noqa: E402

from cairnml import Task  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="upload one artifact of every kind")
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--work", required=True, help="a folder for the files this script creates")
    parser.add_argument("--expect", required=True, help="where the expected digests are written")
    parser.add_argument("--big-mb", type=int, default=0, help="also upload a file of this size (0: skip)")
    args = parser.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    kinds = {}

    model_file = work / "model.bin"
    model_file.write_bytes(deterministic_bytes(200_000, seed=1))
    folder = work / "checkpoints"
    folder.mkdir(exist_ok=True)
    (folder / "epoch-1.txt").write_text("epoch one\n")
    (folder / "epoch-2.txt").write_text("epoch two\n")
    meta = {"owner": "e2e", "kind": "metadata"}
    payload = {"name": "run", "values": [1, 2, 3], "nested": {"ok": True}}
    frame = pd.DataFrame({"x": [1, 2, 3], "y": ["a", "b", "c"]})
    array = np.arange(12, dtype=np.float32).reshape(3, 4)
    image = Image.fromarray(np.tile(np.arange(32, dtype=np.uint8), (32, 1)))
    pickled = {"weights": (1, 2.5, "three"), "tags": {"alpha", "beta"}}

    task = Task.init(project_name=args.project, task_name=args.name, reuse_last_task_id=False)
    task.upload_artifact("file", artifact_object=str(model_file), wait_on_upload=True)
    kinds["file"] = ("file", file_digest(model_file))
    task.upload_artifact("folder", artifact_object=str(folder), wait_on_upload=True)
    kinds["folder"] = ("folder", folder_digest(folder))
    task.upload_artifact("dict", artifact_object=payload, wait_on_upload=True)
    kinds["dict"] = ("dict", json_digest(payload))
    task.upload_artifact("frame", artifact_object=frame, wait_on_upload=True)
    kinds["frame"] = ("frame", frame_digest(frame))
    task.upload_artifact("array", artifact_object=array, wait_on_upload=True)
    kinds["array"] = ("array", array_digest(array))
    task.upload_artifact("image", artifact_object=image, wait_on_upload=True)
    kinds["image"] = ("image", image_digest(image))
    task.upload_artifact("pickled", artifact_object=pickled, wait_on_upload=True, auto_pickle=True)
    kinds["pickled"] = ("pickled", pickled_digest(pickled))
    task.upload_artifact("with_metadata", artifact_object=payload, metadata=meta, wait_on_upload=True)
    kinds["with_metadata"] = ("dict", json_digest(payload))

    metadata = {"with_metadata": meta}
    if args.big_mb:
        big = work / "big.bin"
        with open(big, "wb") as handle:
            for index in range(args.big_mb):
                handle.write(deterministic_bytes(1024 * 1024, seed=1000 + index))
        task.upload_artifact("big", artifact_object=str(big), wait_on_upload=True)
        kinds["big"] = ("file", file_digest(big))

    Path(args.expect).write_text(json.dumps({"task_id": task.id, "artifacts": kinds, "metadata": metadata}))
    report({"task_id": task.id, "artifacts": sorted(kinds)})
    task.close()


if __name__ == "__main__":
    main()
