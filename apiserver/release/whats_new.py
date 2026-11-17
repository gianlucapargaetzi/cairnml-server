"""
The "What's new" bundle: the release notes of the server, the web app and the SDK, from their CHANGELOG.md files.

A changelog has one section per release, headed "## <version>" (the server's "## API <version>" sections are not
release notes and are left out). The bundle is what the web app reads to show what changed since a user last saw a
release:

    {"server": {"0.0.6": "text", ...}, "web": {...}, "sdk": {...}}

Run: python -m apiserver.release.whats_new --server CHANGELOG.md --web <file> --sdk <file> --out whats-new.json
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Optional

VERSION_HEADING = re.compile(r"^## (\d+\.\d+\.\d+)\s*$", re.M)
COMPONENTS = ("server", "web", "sdk")


def sections(text: str) -> Dict[str, str]:
    """The release sections of a changelog: version -> the text under its heading (until the next heading of any level)."""
    found = {}
    matches = list(VERSION_HEADING.finditer(text))
    for match in matches:
        end = len(text)
        following = re.search(r"^#{1,2} ", text[match.end() :], re.M)
        if following:
            end = match.end() + following.start()
        body = text[match.end() : end].strip()
        found[match.group(1)] = body
    return found


def bundle(changelogs: Dict[str, Optional[str]]) -> dict:
    """The bundle of the given changelog texts, by component; a component without a changelog is an empty mapping."""
    return {name: sections(changelogs[name]) if changelogs.get(name) else {} for name in COMPONENTS}


def has_section(changelog: str, version: str) -> bool:
    """Whether a changelog has the release section of a version (the release check requires it for a release)."""
    return version in sections(changelog)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apiserver.release.whats_new", description=__doc__)
    for name in COMPONENTS:
        parser.add_argument(f"--{name}", type=Path, help=f"the {name} changelog")
    parser.add_argument("--out", type=Path, help="the bundle to write")
    parser.add_argument("--notes", type=Path, help="print the notes of one release from this changelog")
    parser.add_argument("--version", help="the release whose notes --notes prints")
    args = parser.parse_args(argv)
    if args.notes:
        # the release notes of one version, for a GitHub release: an empty text when the changelog has no section
        text = args.notes.read_text() if args.notes.exists() else ""
        print(sections(text).get(args.version or "", ""))
        return 0
    if args.out is None:
        parser.error("--out is required unless --notes is given")
    texts = {
        name: getattr(args, name).read_text() if getattr(args, name) and getattr(args, name).exists() else None
        for name in COMPONENTS
    }
    args.out.write_text(json.dumps(bundle(texts), indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
