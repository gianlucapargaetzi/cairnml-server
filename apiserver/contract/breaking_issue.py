"""The follow-up issue the sync workflow opens in the SDK, the web app and the agent for a breaking API change.

The decisions are made here, not in shell: whether a change report has a breaking change, the title and body of the issue
(from the JSON and markdown reports of ``python -m apiserver.contract.evolution``), and what an earlier issue for the same
API version means. One issue is opened per API version, ever: an open one gets a comment, a closed one means the
maintainer has worked through it and nothing is done, none means create it.

Commands (the workflow passes files, a sha and a note; nothing from the schema reaches shell text)::

    python -m apiserver.contract.breaking_issue plan --json-report F --section-file F --changelog F --sha SHA
        --note TEXT --out-dir DIR      prints "breaking <version>" and writes title.txt, body.md and comment.md into DIR,
                                       or prints "none" and writes nothing
    python -m apiserver.contract.breaking_issue find --json-report F --issues-file F
                                       prints "open <number>", "closed <number>" or "none" for the issues of the label
                                       (the file is the output of `gh api --paginate` on the issues endpoint)
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from apiserver.contract.evolution import changelog_link
from apiserver.contract.pr_body import SHA as SHA_PATTERN
from apiserver.contract.pr_body import build_body as build_text

VERSION = re.compile(r"[0-9]+\.[0-9]+")
TITLE_START = "API breaking change"
MAX_TITLE = 200  # GitHub accepts 256; the changelog of one API version can name more than twenty endpoints
MAX_NAMED = 5

CHECKLIST = """- [ ] Adopt the new definition where it brings functionality we want.
- [ ] Check that the old definition is still supported by this repository's code.
- [ ] Decide whether anything is dropped."""


def _version(value) -> str:
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError(f"not an API version: {value!r}")
    return value


def _sha(value) -> str:
    if not isinstance(value, str) or not SHA_PATTERN.match(value):
        raise ValueError(f"not a full lower-case commit sha: {value!r}")
    return value


def _plain(text) -> str:
    """One line of printable characters, for a title."""
    return "".join(ch for ch in " ".join(str(text).split()) if ch.isprintable())


def title_prefix(top: str) -> str:
    """The start of every title for an API version; it ends at the colon, so 2.4 never matches 2.41."""
    return f"{TITLE_START} {_version(top)}:"


def load_report(path) -> dict:
    """The JSON report of the contract tool, checked for the parts used here."""
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the report {path}: {error}") from error
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("changes"), list)
        or not all(isinstance(c, dict) and isinstance(c.get("endpoint"), str) for c in report["changes"])
    ):
        raise ValueError(f"{path} is not a change report of the contract tool")
    _version(report.get("top"))
    _version(report.get("base_api"))
    return report


def breaking_endpoints(report: dict) -> List[str]:
    """The endpoints with at least one breaking change, each once, in the order of the report."""
    seen: Dict[str, None] = {}
    for change in report["changes"]:
        if change.get("breaking") is True:
            seen.setdefault(_plain(change["endpoint"]), None)
    return list(seen)


def build_title(report: dict) -> str:
    """``API breaking change <top>: <first 5 endpoints>[ and N more]``, at most 200 characters."""
    endpoints = breaking_endpoints(report)
    if not endpoints:
        raise ValueError("the report has no breaking change")
    prefix = f"{title_prefix(report['top'])} "
    for count in range(min(MAX_NAMED, len(endpoints)), 0, -1):
        rest = len(endpoints) - count
        title = prefix + ", ".join(endpoints[:count]) + (f" and {rest} more" if rest else "")
        if len(title) <= MAX_TITLE:
            return title
    rest = len(endpoints) - 1
    suffix = f" and {rest} more" if rest else ""
    room = MAX_TITLE - len(prefix) - len(suffix) - 3
    return prefix + endpoints[0][: max(room, 0)] + "..." + suffix


def find_issue(issues: List[dict], top: str) -> Tuple[str, Optional[int]]:
    """What an earlier issue for the API version means: ("open", n) for the lowest-numbered open one, ("closed", n) when
    there is only a closed one (the maintainer has worked through it), ("none", None) otherwise."""
    prefix = title_prefix(top)
    # the REST endpoint lists pull requests as issues too: they carry a pull_request key and are never the issue
    found = [
        i
        for i in issues
        if "pull_request" not in i and isinstance(i.get("title"), str) and i["title"].startswith(prefix)
    ]
    open_numbers = sorted(i["number"] for i in found if str(i.get("state", "")).upper() == "OPEN")
    if open_numbers:
        return "open", open_numbers[0]
    closed_numbers = sorted(i["number"] for i in found)
    if closed_numbers:
        return "closed", closed_numbers[0]
    return "none", None


def load_listing(path, what: str = "issue listing") -> List[dict]:
    """The issues listed by ``gh api --paginate`` (the REST endpoint): one JSON array per page, one after the other, or a
    single array. An empty file, an error object or anything else is refused, never read as "no issues". ``what`` names
    the listing in the error, so that the comments of a pull request can be read the same way."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read the {what} {path}: {error}") from error
    decoder = json.JSONDecoder()
    issues: List[dict] = []
    position, pages = 0, 0
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        try:
            page, position = decoder.raw_decode(text, position)
        except ValueError as error:
            raise ValueError(f"the {what} {path} is not JSON: {error}") from error
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise ValueError(f"the {what} {path} is not a list of objects")
        issues.extend(page)
        pages += 1
    if pages == 0:
        raise ValueError(f"the {what} {path} is empty")
    return issues


def _link_text(link: str) -> str:
    return f"[CHANGELOG]({link})" if link.startswith("https://") else link


def build_body(report: dict, section: str, sha: str, link: str, note: str) -> str:
    """The body of the issue: what happened, the checklist, and the change list. Below GitHub's size limit."""
    top, base_api = _version(report["top"]), _version(report["base_api"])
    intro = (
        f"The API of cairnml-server has a breaking change: API `{base_api}` to `{top}`, at cairnml-server@{_sha(sha)}. "
        f"Changelog: {_link_text(link)}.\n\n"
        f"Decide what this repository does about it:\n\n{CHECKLIST}\n\n"
        f"This issue is for API {top} only: a later change of the same version is added as a comment. Close it when the "
        "checklist is worked through; nothing closes it automatically."
    )
    return build_text(intro, section, None, note)


def build_comment(report: dict, section: str, sha: str, note: str) -> str:
    """The comment for a later run that finds the issue of the API version open."""
    top = _version(report["top"])
    intro = f"Another run for API `{top}`, at cairnml-server@{_sha(sha)}:"
    return build_text(intro, section, None, note)


def _plan(args) -> int:
    report = load_report(args.json_report)
    if not breaking_endpoints(report):
        print("none")
        return 0
    section = Path(args.section_file).read_text(encoding="utf-8")
    changelog = Path(args.changelog).read_text(encoding="utf-8") if Path(args.changelog).exists() else ""
    link = changelog_link(args.sha, report["top"], changelog)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "title.txt").write_text(build_title(report), encoding="utf-8")
    (out / "body.md").write_text(build_body(report, section, args.sha, link, args.note or ""), encoding="utf-8")
    (out / "comment.md").write_text(build_comment(report, section, args.sha, args.note or ""), encoding="utf-8")
    print(f"breaking {report['top']}")
    return 0


def _find(args) -> int:
    report = load_report(args.json_report)
    issues = load_listing(args.issues_file)
    state, number = find_issue(issues, report["top"])
    print(state if number is None else f"{state} {number}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apiserver.contract.breaking_issue", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--json-report", required=True)
    plan.add_argument("--section-file", required=True)
    plan.add_argument("--changelog", required=True)
    plan.add_argument("--sha", required=True)
    plan.add_argument("--note")
    plan.add_argument("--out-dir", required=True)
    plan.set_defaults(run=_plan)
    find = sub.add_parser("find")
    find.add_argument("--json-report", required=True)
    find.add_argument("--issues-file", required=True)
    find.set_defaults(run=_find)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
