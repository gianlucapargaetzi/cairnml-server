"""
The server no longer reads or writes the names of the ClearML era (TRAINS_*, /opt/trains, the trains prefix, Allegro).
Lines that keep one on purpose are listed here with the reason, so a new occurrence fails this test.
"""

import re
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
LEGACY = re.compile(r"trains|allegro", re.IGNORECASE)
SKIP_DIRS = {"tests", "__pycache__", "dist", "egg-info"}

# (relative path, line stripped) -> reason
ALLOWED = {}


def _source_files():
    for path in SERVER_ROOT.rglob("*"):
        if path.suffix not in {".py", ".conf"} or not path.is_file():
            continue
        if SKIP_DIRS & set(path.relative_to(SERVER_ROOT).parts):
            continue
        yield path


def _offending_lines():
    found = []
    for path in _source_files():
        rel = path.relative_to(SERVER_ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if LEGACY.search(line) and (rel, line.strip()) not in ALLOWED:
                found.append(f"{rel}:{number}: {line.strip()}")
    return found


def test_no_legacy_names_outside_the_allow_list():
    assert _offending_lines() == []


@pytest.mark.parametrize("key", sorted(ALLOWED))
def test_every_allowed_line_still_exists(key):
    rel, line = key
    assert line in (SERVER_ROOT / rel).read_text(encoding="utf-8"), f"{rel} no longer has the allowed line"
