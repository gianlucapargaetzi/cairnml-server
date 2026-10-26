"""Helpers shared by the example scripts: the one JSON line a script prints at the end, and the digests the
artifact scripts compare (the upload side writes them, the check side recomputes them from what came back)."""

import faulthandler
import hashlib
import io
import json
import os
import sys
import threading
from pathlib import Path

# A script that hangs (the client does not exit) dumps the stack of every thread to stderr, for the test's report.
faulthandler.dump_traceback_later(90, repeat=False, file=sys.stderr)


_exit_timer = None


def report(result: dict) -> None:
    """The last line of every script: what it did, as JSON, for the test that ran it.

    The SDK sometimes does not let the process exit after its work is done (seen in the suite as a hang at exit).
    The script is ended 30 s after its report, so a test does not wait for that: the work itself is finished before
    the report, as the scripts close their tasks first. The hang itself is still open (see docs/e2e_testing)."""
    print(json.dumps(result, sort_keys=True), flush=True)
    # a batch reports once per run: the exit is 30 s after the last report
    global _exit_timer
    if _exit_timer is not None:
        _exit_timer.cancel()
    _exit_timer = threading.Timer(30, os._exit, [0])
    _exit_timer.start()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path) -> str:
    return sha256(Path(path).read_bytes())


def folder_digest(path) -> str:
    """Digest of a folder: its relative file names and their contents, in a stable order."""
    root = Path(path)
    entries = []
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            full = Path(dirpath) / filename
            entries.append((full.relative_to(root).as_posix(), file_digest(full)))
    return sha256(json.dumps(sorted(entries)).encode())


def json_digest(obj) -> str:
    return sha256(json.dumps(obj, sort_keys=True).encode())


def frame_digest(frame) -> str:
    return sha256(frame.to_csv(index=False).encode())


def array_digest(array) -> str:
    import numpy as np

    array = np.asarray(array)
    return sha256(str(array.dtype).encode() + str(array.shape).encode() + array.tobytes())


def image_digest(image) -> str:
    """The pixels of an image (PIL or array), so the digest does not depend on the file format."""
    import numpy as np

    if hasattr(image, "convert"):
        image = np.asarray(image.convert("RGB"))
    return array_digest(image)


def pickled_digest(obj) -> str:
    """The canonical form of a plain object (dicts, lists, tuples, sets, numbers, strings)."""
    return sha256(repr(_canonical(obj)).encode())


def _canonical(obj):
    if isinstance(obj, dict):
        return sorted((repr(k), _canonical(v)) for k, v in obj.items())
    if isinstance(obj, (set, frozenset)):
        return sorted(repr(_canonical(v)) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [type(obj).__name__, [_canonical(v) for v in obj]]
    return obj


def deterministic_bytes(size: int, seed: int) -> bytes:
    """Bytes that are the same on every run for the same seed, so a re-run can be compared."""
    out = io.BytesIO()
    block = hashlib.sha256(str(seed).encode()).digest()
    while out.tell() < size:
        block = hashlib.sha256(block).digest()
        out.write(block)
    return out.getvalue()[:size]
