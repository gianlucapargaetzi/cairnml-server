"""A copy of clearml/datasets/footprint.py in the SDK, which defines the footprint. Keep the two the same: the server
uses it to check a dataset repository before it is registered. (The golden-vector test in both suites catches drift.)

The footprint of a dataset repository: a record that a dataset was checked, which anyone can verify from git alone.

``clearml-dataset check`` validates the data and writes ``dataset.lock.json`` next to ``data.dvc``. The lock copies the
size, file count and content hash that DVC wrote to ``data.dvc``, says in which format the data was read (native, or
through an adapter), and ends with a checksum of everything above it. ``verify`` recomputes what it can from files that
are in git, so it needs no data, no server and no key:

- ``data.dvc`` and the lock agree, so the data has not changed since the check
- the checksum matches the lock's content, so the lock was not edited by hand without recomputing it
- ``dataset.yaml`` and the lock name the same format
- the repository holds only the files a dataset repository may hold, none of them large

It is not a signature. Someone who writes a matching lock by hand gets past it; that fails when the dataset is used.

This file uses only the standard library and does not import the rest of the SDK, because ``clearml-dataset init``
copies it to ``.clearml/verify.py`` in each dataset repository, where a GitHub Action runs it as a script.
"""

import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional

FOOTPRINT_VERSION = 1
LOCK_FILE = "dataset.lock.json"
CONFIG_FILE = "dataset.yaml"
DVC_FILE = "data.dvc"
VERIFY_SCRIPT = ".clearml/verify.py"
MAX_TRACKED_BYTES = 1024 * 1024

# What may be tracked by git at the top of a dataset repository. The data itself is in DVC.
ALLOWED_TOP_LEVEL = {
    DVC_FILE, CONFIG_FILE, LOCK_FILE, ".gitignore", ".dvcignore", ".dvc", ".github", ".clearml",
    "README.md", "README", "LICENSE", "LICENSE.md", "NOTICE", "docs",
}

_SHA = re.compile(r"[0-9a-f]{7,40}")


# Reading the two small text files, without a YAML library


def parse_dvc_file(text: str) -> Dict[str, Any]:
    """``hash``, ``size`` and ``nfiles`` of the first output in a ``data.dvc`` file."""
    found: Dict[str, Any] = {}
    for key in ("md5", "size", "nfiles"):
        match = re.search(rf"^\s*-?\s*{key}:\s*(\S+)\s*$", text, re.M)
        if match:
            found[key] = match.group(1)
    if "md5" not in found or "size" not in found or "nfiles" not in found:
        raise ValueError("data.dvc has no md5, size and nfiles for its output")
    return {"hash": found["md5"], "size": int(found["size"]), "nfiles": int(found["nfiles"])}


def parse_config(text: str) -> Dict[str, str]:
    """``key: value`` lines of ``dataset.yaml``. Comments and blank lines are ignored."""
    config: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"dataset.yaml: cannot read the line {line!r}")
        key, value = line.split(":", 1)
        config[key.strip()] = value.strip().strip("'\"")
    return config


def config_format(config: Mapping[str, str]) -> Dict[str, str]:
    """The format ``dataset.yaml`` asks for: ``{"kind": "native"}`` or ``{"kind": "adapter", "name": ..., ...}``."""
    kind, adapter = config.get("format"), config.get("adapter")
    if adapter and kind not in (None, "adapter"):
        raise ValueError("dataset.yaml sets both 'format: native' and an adapter; use one of them")
    if adapter:
        result = {"kind": "adapter", "name": adapter}
        if config.get("registry"):
            result["registry"] = config["registry"]
        return result
    if kind == "native":
        return {"kind": "native"}
    raise ValueError("dataset.yaml must say 'format: native' or name an 'adapter: <name>'")


# The footprint


def checksum(body: Mapping[str, Any]) -> str:
    """sha256 of the lock's content, as canonical JSON, without its own ``footprint`` field."""
    content = {key: value for key, value in body.items() if key != "footprint"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_lock(
    dvc: Mapping[str, Any],
    fmt: Mapping[str, str],
    samples: int,
    modalities: Mapping[str, str],
    labels: Mapping[str, str],
    tool_version: str,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "version": FOOTPRINT_VERSION,
        "dvc": dict(dvc),
        "format": dict(fmt),
        "samples": int(samples),
        "modalities": dict(modalities),
        "labels": dict(labels),
        "tool_version": tool_version,
    }
    body["footprint"] = checksum(body)
    return body


def _blank(files: Mapping[str, Optional[bytes]], name: str) -> bool:
    return files.get(name) is None


def verify_files(files: Mapping[str, Optional[bytes]], sizes: Mapping[str, int]) -> List[str]:
    """What is wrong with a dataset repository, as sentences that say what to do. Empty when it is fine.

    :param files: the content of ``data.dvc``, ``dataset.yaml`` and ``dataset.lock.json`` (None when missing)
    :param sizes: size in bytes of every file tracked by git, by path
    """
    problems: List[str] = []
    for name, hint in ((DVC_FILE, "run 'clearml-dataset add'"), (CONFIG_FILE, "run 'clearml-dataset init'"),
                       (LOCK_FILE, "run 'clearml-dataset commit' to check the data and write it")):
        if _blank(files, name):
            problems.append(f"{name} is missing: {hint}")
    if problems:
        return problems

    try:
        dvc = parse_dvc_file(files[DVC_FILE].decode())
        wanted = config_format(parse_config(files[CONFIG_FILE].decode()))
        lock = json.loads(files[LOCK_FILE].decode())
    except (ValueError, UnicodeDecodeError) as exc:
        return [f"cannot read the dataset files: {exc}"]
    if not isinstance(lock, dict):
        return [f"{LOCK_FILE} is not a JSON object"]

    if lock.get("version") != FOOTPRINT_VERSION:
        problems.append(f"{LOCK_FILE} has version {lock.get('version')!r}; this tool reads {FOOTPRINT_VERSION}")
    if lock.get("footprint") != checksum(lock):
        problems.append(f"the checksum in {LOCK_FILE} does not match its content: it was edited; run 'clearml-dataset check'")
    if lock.get("dvc") != dvc:
        problems.append(
            f"{LOCK_FILE} was written for other data than {DVC_FILE} holds "
            f"(lock: {lock.get('dvc')}, data.dvc: {dvc}): the data changed after the check; run 'clearml-dataset commit'"
        )
    fmt = lock.get("format") or {}
    if fmt.get("kind") != wanted.get("kind") or fmt.get("name") != wanted.get("name"):
        problems.append(f"{LOCK_FILE} was made for format {fmt} but {CONFIG_FILE} asks for {wanted}; run 'clearml-dataset check'")
    if fmt.get("kind") == "adapter" and not _SHA.fullmatch(str(fmt.get("registry_commit", ""))):
        problems.append(f"{LOCK_FILE} does not say which commit of the adapter registry was used")
    if not isinstance(lock.get("samples"), int) or lock["samples"] < 1:
        problems.append(f"{LOCK_FILE} reports no samples: the data was not read into the standard format")

    for path, size in sorted(sizes.items()):
        top = path.split("/", 1)[0]
        if top not in ALLOWED_TOP_LEVEL:
            problems.append(f"{path} is tracked by git; only the dataset files may be (put data in data/, which DVC tracks)")
        elif top == "data":
            problems.append(f"{path} is tracked by git; data/ belongs to DVC")
        elif size > MAX_TRACKED_BYTES:
            problems.append(f"{path} is {size} bytes in git; large files belong in data/")
    return problems


def tracked_sizes(root: str) -> Dict[str, int]:
    """Size of every file under ``root``, without the git folder and the DVC content store, by relative path."""
    sizes: Dict[str, int] = {}
    skip = {".git", ".venv", "__pycache__", "data"}
    for folder, dirs, names in os.walk(root):
        rel = os.path.relpath(folder, root)
        dirs[:] = [d for d in dirs if not (rel == "." and d in skip)
                   and not (rel.startswith(".dvc") and d in ("cache", "tmp"))]
        if rel.startswith(".dvc"):
            continue
        for name in names:
            if rel == "." and name in (".ready", ".last_used"):  # markers of the dataset cache, not of the repository
                continue
            path = os.path.normpath(os.path.join(rel, name)).replace(os.sep, "/")
            sizes[path] = os.path.getsize(os.path.join(folder, name))
    return sizes


def read_files(root: str) -> Dict[str, Optional[bytes]]:
    files: Dict[str, Optional[bytes]] = {}
    for name in (DVC_FILE, CONFIG_FILE, LOCK_FILE):
        path = os.path.join(root, name)
        files[name] = open(path, "rb").read() if os.path.isfile(path) else None
    return files


def verify(root: str = ".") -> List[str]:
    """Verify the dataset repository in a folder. ``root`` is a checkout of the repository, not of its data."""
    sizes = {p: s for p, s in tracked_sizes(root).items() if not p.startswith(".dvc/")}
    return verify_files(read_files(root), sizes)


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = args[0] if args else "."
    problems = verify(root)
    if problems:
        print("This dataset repository does not pass its checks:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("The dataset footprint is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
