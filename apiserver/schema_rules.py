"""Rules of the API schema that every reader enforces: the server, the client generator and the API change check.

This module imports nothing but the standard library, so the client generator can use it without the server's
configuration.
"""

import re
from typing import Iterable, List, Tuple

MAX_DEFINITIONS_PER_ENDPOINT = 5
"""The most definitions (version blocks) an endpoint may keep. The cap is per endpoint, not for the whole API."""

# A definition is written as a block (`"2.9" {`) or as a reference (`"2.9": ${get_all."2.1"}`), and an action header as
# `name {` or `name: {`; HOCON reads all of them, so the scan must see all of them.
VERSION_KEY = re.compile(r'^\s*"?(\d+\.\d+)"?\s*[:{]')
ACTION_HEADER = re.compile(r'^\s*"?([\w.-]+)"?\s*[:{]')
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMMENT = re.compile(r'("(?:[^"\\]|\\.)*")|(//|#).*$')


def check_definition_cap(action: str, versions: Iterable[str]) -> None:
    """Raises ValueError when an action keeps more definitions than the cap allows."""
    count = len(list(versions))
    if count > MAX_DEFINITIONS_PER_ENDPOINT:
        raise ValueError(
            f"{action} keeps {count} definitions; the cap is {MAX_DEFINITIONS_PER_ENDPOINT} per endpoint "
            "(retire the oldest definition before adding one)"
        )


def duplicate_definitions(text: str) -> List[Tuple[str, str]]:
    """(action, version) for every version block written twice in one action of a schema file.

    HOCON merges duplicate keys when it parses, so a file with two blocks of one version would load as one block without
    any error. The file text is read here instead: a version key is a line at the top level of an action (depth 1), and
    each action is a block at depth 0.
    """
    found: List[Tuple[str, str]] = []
    seen = {}
    depth = 0
    action = None
    frame = 0
    for line in text.splitlines():
        code = _COMMENT.sub(lambda m: m.group(1) or "", line)
        if depth == 1:
            match = VERSION_KEY.match(code)
            if match:
                versions = seen.setdefault(frame, set())
                if match.group(1) in versions:
                    found.append((action, match.group(1)))
                versions.add(match.group(1))
        braces = _STRING.sub('""', code)
        for char in braces:
            if char == "{":
                if depth == 0:
                    frame += 1
                    header = ACTION_HEADER.match(code)
                    action = header.group(1) if header else None
                depth += 1
            elif char == "}":
                depth -= 1
    return found
