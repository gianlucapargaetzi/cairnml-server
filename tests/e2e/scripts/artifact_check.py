"""Reads the artifacts of a task back, as a second machine would, and compares each with what was uploaded.

Run by the test in a new process, with the digests written by artifact_upload.py (--expect). Prints one JSON line:
for each artifact, whether its content matches, and the artifact's recorded hash and metadata.
"""

import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import array_digest, file_digest, folder_digest, frame_digest, image_digest  # noqa: E402
from common import json_digest, pickled_digest, report  # noqa: E402

from cairnml import Task  # noqa: E402


def digest_of(kind: str, artifact) -> str:
    if kind == "file":
        return file_digest(artifact.get_local_copy())
    if kind == "folder":
        local = Path(artifact.get_local_copy())
        if local.is_file() and zipfile.is_zipfile(local):
            extracted = Path(tempfile.mkdtemp(prefix="e2e-folder-"))
            try:
                with zipfile.ZipFile(local) as archive:
                    archive.extractall(extracted)
                return folder_digest(extracted)
            finally:
                shutil.rmtree(extracted, ignore_errors=True)
        return folder_digest(local)
    value = artifact.get()
    if kind == "dict":
        return json_digest(value)
    if kind == "frame":
        return frame_digest(value)
    if kind == "array":
        return array_digest(value)
    if kind == "image":
        return image_digest(value)
    if kind == "pickled":
        return pickled_digest(value)
    raise ValueError(f"unknown kind {kind}")


def main() -> None:
    parser = argparse.ArgumentParser(description="read artifacts back and compare them")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expect", required=True)
    args = parser.parse_args()

    import json

    expected = json.loads(Path(args.expect).read_text())
    task = Task.get_task(task_id=args.task_id)
    stored = task.artifacts
    results = {}
    for name, (kind, want) in expected["artifacts"].items():
        if name not in stored:
            results[name] = {"ok": False, "reason": "missing"}
            continue
        artifact = stored[name]
        got = digest_of(kind, artifact)
        results[name] = {"ok": got == want, "kind": kind, "hash": artifact.hash,
                         "metadata": artifact.metadata or {}}
    report({"task_id": args.task_id, "results": results, "names": sorted(stored.keys())})


if __name__ == "__main__":
    main()
