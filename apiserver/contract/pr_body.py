"""The body of a regeneration pull request of the sync workflow (and the part of an issue body that is the same).

A body is the intro, a hidden marker that records the server commit the change list starts from, and the "What changed
in the API" section made by ``python -m apiserver.contract.evolution --markdown-report``. The first run of a rolling pull
request writes the marker; every later run reads it back from the open pull request, so the one section stays cumulative
from a fixed base. A marker is only a hint from text anyone can edit: it counts only as a line of its own with a full
lower-case sha, and the workflow still checks that the commit exists and is an ancestor before it uses it.

Commands (the workflow passes files and a sha, never text)::

    python -m apiserver.contract.pr_body read-marker BODY_FILE
    python -m apiserver.contract.pr_body read-note BODY_FILE
    python -m apiserver.contract.pr_body build --intro-file F --section-file F [--base SHA] [--note TEXT] --out FILE
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional

# GitHub refuses a body of 65536 characters or more; the margin is for the marker and the note
BODY_LIMIT = 60000
SHA = re.compile(r"^[0-9a-f]{40}$")
MARKER_LINE = re.compile(r"^<!-- api-sync-base:(\S*) -->[ \t]*$")
MARKER_PREFIX = "<!-- api-sync-base:"
LEFT_OUT = "_{n} more row(s) left out to fit the size limit: see the generated diff_"
TRUNCATED = "\n\n_(section truncated to fit the size limit)_"


def read_marker(body: str) -> Optional[str]:
    """The base sha a body records, or None. The first line that starts a marker decides: when it is not a well-formed
    marker with a full lower-case sha, there is no base (a later line cannot replace it)."""
    for line in body.splitlines():
        if line.startswith(MARKER_PREFIX) or MARKER_PREFIX in line:
            match = MARKER_LINE.match(line)
            if match is None or not SHA.match(match.group(1)):
                return None
            return match.group(1)
    return None


NOTE_LINE = re.compile(r"^_Compared from ([0-9a-f]{8})\. (Base: .{1,300})_$")


def read_note(body: str) -> Optional[str]:
    """The reason for the base ("Base: ...") that a body states under its section, so that a later run that keeps the
    recorded base writes the same text. Only a body with a valid marker has one, and only the last line of the form
    ``_Compared from <first 8 characters of the marker>. Base: <text>_`` counts."""
    base = read_marker(body)
    if base is None:
        return None
    note = None
    for line in body.splitlines():
        match = NOTE_LINE.match(line.rstrip())
        if match and match.group(1) == base[:8]:
            note = match.group(2)
    return note


def _is_row(line: str) -> bool:
    return line.startswith("| ") and not line.startswith("| ---") and not line.startswith("| Endpoint")


def _assemble(intro: str, section: str, base: Optional[str], note: str) -> str:
    parts = [intro.strip()]
    if base:
        parts.append(f"{MARKER_PREFIX}{base} -->")
    parts.append(section.strip())
    if note:
        parts.append(f"_{note.strip()}_")
    return "\n\n".join(part for part in parts if part) + "\n"


def build_body(intro: str, section: str, base: Optional[str], note: str = "", limit: int = BODY_LIMIT) -> str:
    """The body: intro, marker (when a base is given), section, note. A body over the limit loses table rows from the
    end first and says so; a section without rows is cut as the last resort. Never refuses for length."""
    if base is not None and not SHA.match(base):
        raise ValueError(f"the base must be a full lower-case commit sha, not {base!r}")
    if base:
        note = f"Compared from {base[:8]}. {note}".strip()
    body = _assemble(intro, section, base, note)
    if len(body) <= limit:
        return body
    lines: List[str] = section.strip().splitlines()
    rows = [i for i, line in enumerate(lines) if _is_row(line)]
    for keep in range(len(rows) - 1, -1, -1):
        first_dropped = rows[keep]
        rest = [line for line in lines[first_dropped:] if not _is_row(line)]
        shortened = lines[:first_dropped] + ["", LEFT_OUT.format(n=len(rows) - keep)] + rest
        body = _assemble(intro, "\n".join(shortened), base, note)
        if len(body) <= limit:
            return body
    # no rows left to drop (or none to begin with) and still too long: cut the section itself
    text = "\n".join(lines)
    room = max(limit - len(_assemble(intro, TRUNCATED, base, note)), 0)
    body = _assemble(intro, text[:room].rstrip() + TRUNCATED, base, note)
    while len(body) > limit and room > 0:  # the strip of the cut text can shift a character or two
        room = max(room - (len(body) - limit), 0)
        body = _assemble(intro, text[:room].rstrip() + TRUNCATED, base, note)
    return body


def _read_marker_command(args) -> int:
    base = read_marker(Path(args.file).read_text(encoding="utf-8", errors="replace"))
    if base:
        print(base)
    return 0


def _read_note_command(args) -> int:
    note = read_note(Path(args.file).read_text(encoding="utf-8", errors="replace"))
    if note:
        print(note)
    return 0


def _build_command(args) -> int:
    try:
        body = build_body(
            Path(args.intro_file).read_text(encoding="utf-8"),
            Path(args.section_file).read_text(encoding="utf-8"),
            args.base,
            args.note or "",
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    Path(args.out).write_text(body, encoding="utf-8")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apiserver.contract.pr_body", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read-marker", help="print the base sha recorded in a body file, nothing when there is none")
    read.add_argument("file")
    read.set_defaults(run=_read_marker_command)
    note = sub.add_parser("read-note", help="print the reason for the base a body states, nothing when there is none")
    note.add_argument("file")
    note.set_defaults(run=_read_note_command)
    build = sub.add_parser("build", help="write a body file")
    build.add_argument("--intro-file", required=True)
    build.add_argument("--section-file", required=True)
    build.add_argument("--base", help="the full sha the section starts from (omit it for an issue)")
    build.add_argument("--note", help="why this base was used")
    build.add_argument("--out", required=True)
    build.set_defaults(run=_build_command)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
