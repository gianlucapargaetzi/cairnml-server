"""Checks how the API changes between two releases, and whether the change is allowed.

The contract of an endpoint at an API version is its version block in the schema, or, when that version has no block of
its own, the newest block below it. A version block marked ``removed = true`` means the endpoint does not exist at that
version. Comparing the contract at the base API version with the contract at the head API version gives the change:

- an endpoint that did not exist and exists now is added (never blocks);
- an endpoint that existed and does not exist now is removed (breaking);
- an endpoint whose request or response changed is changed; each change to a field is classified:
  a removed field, a field that became required, a changed type, or an allowed value that is gone is breaking;
  a new optional field, a new allowed value or a field that is no longer required is additive.

A breaking change is allowed only when the API version is raised and the changelog has a section ``## API <head>``
that names the endpoint. An endpoint whose request or response differs from its newest definition in the base needs a
definition of its own labelled newer than that one, even for an additive change; comments and descriptions do not count.
Run it from the repository root, against the commit to compare with::

    python -m apiserver.contract.evolution --base-ref origin/main

``--markdown-report FILE`` writes the change list as a markdown section (the "What changed in the API" part of the
regeneration pull requests of the sync workflow) and ``--json-report FILE`` writes it as JSON; both are written after the
check, also when it finds problems, and the text output and the exit code stay as they are. A base that cannot be read
writes no report.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pyhocon import ConfigFactory
from pyhocon.exceptions import ConfigException
from pyparsing import ParseBaseException

from apiserver.schema_rules import check_definition_cap, duplicate_definitions

VERSION = re.compile(r"^\d+\.\d+$")
MAX_DEPTH = 12
REPO_ROOT = Path(__file__).parents[2]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def vkey(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _plain(value):
    if isinstance(value, dict):
        return {str(k).strip('"'): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _without_descriptions(value):
    if isinstance(value, dict):
        return {k: _without_descriptions(v) for k, v in value.items() if k != "description"}
    if isinstance(value, list):
        return [_without_descriptions(v) for v in value]
    return value


@dataclass
class Endpoint:
    versions: Dict[str, dict]  # version -> block (descriptions removed)
    action: dict  # the settings of the action that are not versions (authorize, allow_roles, internal)
    definitions: dict  # the definitions of the service, which the blocks refer to


def load_api(schema_root: Path) -> Dict[str, Endpoint]:
    """Every endpoint of the schema folder, by its full name ``service.action``. Raises ValueError for a file with two
    definitions of one version in one action, which the parsed result would hide."""
    api: Dict[str, Endpoint] = {}
    for path in sorted(Path(schema_root).glob("*.conf")):
        if path.name.startswith("_"):
            continue
        duplicates = duplicate_definitions(path.read_text(encoding="utf-8"))
        if duplicates:
            listed = ", ".join(f"{action} {version}" for action, version in duplicates)
            raise ValueError(f"{path.name}: two definitions of one version in one action: {listed}")
        conf = _plain(ConfigFactory.parse_file(str(path), resolve=True))
        definitions = conf.get("_definitions") or {}
        for action, body in conf.items():
            if action.startswith("_") or not isinstance(body, dict):
                continue
            versions = {k: _without_descriptions(v) for k, v in body.items() if VERSION.match(k)}
            settings = {k: v for k, v in body.items() if not VERSION.match(k)}
            api[f"{path.stem}.{action}"] = Endpoint(versions, _without_descriptions(settings), definitions)
    return api


def effective(endpoint: Endpoint, version: str) -> Optional[dict]:
    """The block that applies at a version: the newest block at or below it. None when the endpoint does not exist
    there, either because no block applies or because the applying block is marked removed."""
    below = [v for v in endpoint.versions if vkey(v) <= vkey(version)]
    if not below:
        return None
    block = endpoint.versions[max(below, key=vkey)]
    if block.get("removed") is True:
        return None
    return block


@dataclass(frozen=True)
class Change:
    endpoint: str
    kind: str  # added | removed | changed
    breaking: bool
    detail: str
    # the version keys of the definitions the change goes from and to: the newest block at or below the base API version
    # in the base and the newest block at or below the compared version in the head (None when the endpoint did not
    # exist); for a change inside an older definition both are that definition's version
    old_def: Optional[str] = None
    new_def: Optional[str] = None


def _definition_key(endpoint: Endpoint, version: str) -> Optional[str]:
    below = [v for v in endpoint.versions if vkey(v) <= vkey(version)]
    return max(below, key=vkey) if below else None


def _resolve(schema, definitions, depth):
    if depth > MAX_DEPTH or not isinstance(schema, dict) or "$ref" not in schema:
        return schema
    name = schema["$ref"].split("/")[-1]
    return _resolve(definitions.get(name, {}), definitions, depth + 1)


def _union(schema: dict) -> list:
    """The members of a schema that is one of several shapes (its null member excluded), or [] for a single shape."""
    members = schema.get("anyOf") or schema.get("oneOf") or []
    return [m for m in members if m.get("type") != "null"]


def _union_key(schema: dict):
    """What a union is made of, independent of the order of its members and of descriptions."""
    members = _union(schema)
    if not members:
        return None
    return tuple(
        sorted(
            (m.get("$ref") or m.get("type") or "object", tuple(sorted((m.get("properties") or {}).keys())))
            for m in members
        )
    )


def _compare(path, old, new, old_defs, new_defs, is_request, out, depth=0):
    """Appends the changes of a schema fragment to out, as (breaking, text) pairs."""
    if depth > MAX_DEPTH:
        return
    old = _resolve(old, old_defs, 0)
    new = _resolve(new, new_defs, 0)
    if not isinstance(old, dict) or not isinstance(new, dict):
        return
    if old.get("type") is not None and new.get("type") is not None and old.get("type") != new.get("type"):
        out.append((True, f"{path}: type {old.get('type')} became {new.get('type')}"))
    # a field that is one of several shapes (anyOf / oneOf) is a different type from one shape: a client that reads a
    # string gets an object once the field widens, so a change from or to a union, or to other members, is breaking
    old_union, new_union = _union(old), _union(new)
    if (old_union or new_union) and _union_key(old) != _union_key(new):
        before = _union_key(old) if old_union else old.get("type")
        after = _union_key(new) if new_union else new.get("type")
        out.append((True, f"{path}: type {before} became {after}"))
    old_values, new_values = set(old.get("enum") or []), set(new.get("enum") or [])
    for value in sorted(old_values - new_values, key=str):
        out.append((True, f"{path}: value {value!r} is no longer allowed"))
    for value in sorted(new_values - old_values, key=str):
        out.append((False, f"{path}: value {value!r} is allowed"))
    if "properties" in old or "properties" in new:
        old_props, new_props = old.get("properties") or {}, new.get("properties") or {}
        old_req, new_req = set(old.get("required") or []), set(new.get("required") or [])
        for name in sorted(set(old_props) - set(new_props)):
            out.append((True, f"{path}.{name}: field removed"))
        for name in sorted(set(new_props) - set(old_props)):
            if is_request and name in new_req:
                out.append((True, f"{path}.{name}: new required field"))
            else:
                out.append((False, f"{path}.{name}: field added"))
        for name in sorted(set(old_props) & set(new_props)):
            if is_request and name in new_req and name not in old_req:
                out.append((True, f"{path}.{name}: field became required"))
            _compare(f"{path}.{name}", old_props[name], new_props[name], old_defs, new_defs, is_request, out, depth + 1)
    if "items" in old or "items" in new:
        _compare(
            f"{path}[]", old.get("items") or {}, new.get("items") or {}, old_defs, new_defs, is_request, out, depth + 1
        )


def _block_changes(name, base_block, head_block, base_defs, head_defs) -> List[Change]:
    # The fields are compared with their definitions resolved, so a field added to or removed from a shared definition
    # (for example the queue) is found even when the endpoint's own block is the same.
    found: List[Tuple[bool, str]] = []
    for part, is_request in (("request", True), ("response", False)):
        _compare(part, base_block.get(part) or {}, head_block.get(part) or {}, base_defs, head_defs, is_request, found)
    if found:
        return [Change(name, "changed", breaking, text) for breaking, text in found]
    if base_block == head_block:
        return []
    # a difference that is not a field: the contract is not known to be compatible
    return [Change(name, "changed", True, "the contract changed in a way this check does not classify")]


def diff(base: Dict[str, Endpoint], head: Dict[str, Endpoint], base_api: str, head_api: str) -> List[Change]:
    changes: List[Change] = []
    for name in sorted(set(base) | set(head)):
        before = effective(base[name], base_api) if name in base else None
        after = effective(head[name], head_api) if name in head else None
        if before is None and after is None:
            continue
        old_def = _definition_key(base[name], base_api) if name in base else None
        new_def = _definition_key(head[name], head_api) if name in head else None
        if before is None:
            changes.append(Change(name, "added", False, f"new endpoint, from API {head_api}", None, new_def))
            continue
        if after is None:
            changes.append(Change(name, "removed", True, f"endpoint removed at API {head_api}", old_def, new_def))
            continue
        found: List[Change] = []
        if base[name].action != head[name].action:
            found.append(Change(name, "changed", True, "the settings of the endpoint (access, roles) changed"))
        found.extend(_block_changes(name, before, after, base[name].definitions, head[name].definitions))
        changes.extend(replace(change, old_def=old_def, new_def=new_def) for change in found)
        changes.extend(_older_definition_changes(name, base[name], head[name], head_api))
    return changes


def _older_definition_changes(name: str, base: Endpoint, head: Endpoint, head_api: str) -> List[Change]:
    """A breaking change inside an older retained definition (one below the API version of the change), for example
    through a shared definition it refers to: a client that uses that definition would get a different contract. Additive
    changes to it stay additive, as they are for the current definition."""
    changes = []
    for version in sorted(set(base.versions) & set(head.versions), key=vkey):
        if vkey(version) >= vkey(head_api) or base.versions[version] == head.versions[version]:
            continue
        older = _block_changes(name, base.versions[version], head.versions[version], base.definitions, head.definitions)
        for change in older:
            if change.breaking:
                changes.append(Change(name, "changed", True, f"at API {version}: {change.detail}", version, version))
    return changes


def evaluate(changes: List[Change], base_api: str, head_api: str, changelog: str) -> List[str]:
    """The problems of a change: breaking changes need a raised API version and a changelog entry for each endpoint."""
    breaking = [c for c in changes if c.breaking]
    if not breaking:
        return []
    problems = []
    if vkey(head_api) <= vkey(base_api):
        problems += [f"{c.endpoint}: {c.detail} (breaking, but the API version is still {head_api})" for c in breaking]
        return problems
    section = _changelog_section(changelog, head_api)
    if section is None:
        return [f"the changelog has no section '## API {head_api}' for the breaking changes"]
    for c in breaking:
        if not _names_endpoint(section, c.endpoint):
            problems.append(f"{c.endpoint}: {c.detail} is not named in the changelog section 'API {head_api}'")
    return problems


def _names_endpoint(section: str, endpoint: str) -> bool:
    """True when the changelog section names the endpoint as a whole, not as part of a longer name (``devices.exchange``
    is not named by ``devices.exchange_all``)."""
    return re.search(rf"(?<![\w.]){re.escape(endpoint)}(?!\w|\.\w)", section) is not None


def _changelog_section(text: str, version: str) -> Optional[str]:
    heading = f"## API {version}"
    match = re.search(rf"^{re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1) if match else None


VERSION_SOURCES = (
    ("apiserver/service_repo/api_version.py", r'API_VERSION\s*=\s*"([\d.]+)"'),
    # before api_version.py existed, the constant was the maximum version of the registry
    ("apiserver/service_repo/service_repo.py", r'_max_version = PartialVersion\("([\d.]+)"\)'),
)


def _git_show(ref: str, path: str) -> Optional[str]:
    result = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=REPO_ROOT, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _schema_from_ref(ref: str, destination: Path) -> Tuple[Path, str]:
    """The schema folder and the API version of a git ref, exported into destination."""
    archive = subprocess.run(["git", "archive", ref, "apiserver/schema/services"], cwd=REPO_ROOT, capture_output=True)
    if archive.returncode != 0:
        reason = archive.stderr.decode(errors="replace").strip() or f"git archive exited with {archive.returncode}"
        raise ValueError(f"cannot read the schema of {ref}: {reason}")
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive.stdout, check=True)
    for path, pattern in VERSION_SOURCES:
        text = _git_show(ref, path)
        if text is not None and re.search(pattern, text):
            return destination / "apiserver/schema/services", re.search(pattern, text).group(1)
    raise ValueError(f"no API version found in {ref}")


def _change_without_definition(name: str, base: Endpoint, head: Endpoint, top: str) -> Optional[str]:
    """The problem when the request or response of an endpoint differs from its newest definition in the base and no
    definition of the head is labelled newer than that one. A definition that is the same as the one in effect is no
    change; a comment or a description is not part of the contract, so it is no change either."""
    if not base.versions:
        return None
    newest = max(base.versions, key=vkey)
    if base.versions[newest].get("removed") is True:
        return None
    if any(vkey(version) > vkey(newest) for version in head.versions):
        return None
    after = effective(head, top)
    if after is not None and not _block_changes(name, base.versions[newest], after, base.definitions, head.definitions):
        return None
    return f"{name} changed at API {top} without a definition labelled newer than {newest}"


def assess(
    base: Dict[str, Endpoint], head: Dict[str, Endpoint], base_api: str, head_api: str, changelog: str
) -> Tuple[List[Change], List[str], str]:
    """The changes between two schemas, the problems with them, and the API version the changes are for.

    The changes are those of the highest version either schema declares, because a block of a version that is not yet
    the API version would otherwise be ignored. A declared version above the API version is a problem by itself: the
    constant and the schema must be raised together."""
    declared = {v for endpoint in list(base.values()) + list(head.values()) for v in endpoint.versions}
    top = max([head_api] + list(declared), key=vkey)
    problems = []
    if vkey(top) > vkey(head_api):
        problems.append(f"the schema declares API {top} but the API version is {head_api}: raise both together")
    for name in sorted(head):
        try:
            check_definition_cap(name, head[name].versions)
        except ValueError as error:
            problems.append(str(error))
    for name in sorted(set(base) & set(head)):
        problem = _change_without_definition(name, base[name], head[name], top)
        if problem:
            problems.append(problem)
    changes = diff(base, head, base_api, top)
    problems += evaluate(changes, base_api, top, changelog)
    return changes, problems, top


MAX_ROWS = 40
MAX_PROBLEMS = 20
MAX_CELL = 300
REPOSITORY_URL = "https://github.com/gianlucapargaetzi/cairnml-server"


def changelog_link(sha: Optional[str], version: str, changelog: str) -> str:
    """The link to the changelog section of an API version at a commit, or the text "no changelog section" when the
    changelog has no section ``## API <version>`` (or the commit is not known)."""
    if not sha or _changelog_section(changelog, version) is None:
        return "no changelog section"
    return f"{REPOSITORY_URL}/blob/{sha}/CHANGELOG.md#api-{version.replace('.', '')}"


def _code(text: str) -> str:
    """Text as one code span that is safe inside a markdown table cell: no backticks (the span would end), no pipes
    (the cell would end), no line breaks, a bounded length. Inside a code span nothing turns into a mention or link."""
    text = " ".join(str(text).split()).replace("`", "'").replace("|", "\\|")
    if len(text) > MAX_CELL:
        text = text[: MAX_CELL - 3].rstrip("\\") + "..."
    return f"`{text}`"


def _short(ref: str) -> str:
    return ref[:8] if re.fullmatch(r"[0-9a-f]{40}", ref) else ref


def render_markdown(
    changes: List[Change],
    problems: List[str],
    base_api: str,
    top: str,
    base_ref: str,
    head_api: Optional[str] = None,
    changelog_link: Optional[str] = None,
) -> str:
    """The change list as a markdown section for a pull request or an issue: the API versions, the number of breaking
    changes, a table with one row per change (at most MAX_ROWS) and the problems of the check."""
    lines = ["## What changed in the API", ""]
    versions = f"API {_code(base_api)} to {_code(top)}, compared with {_code(_short(base_ref))}."
    if head_api and head_api != top:
        versions += f" The API version of the code is {_code(head_api)}."
    if changelog_link:
        versions += f" Changelog: {'[CHANGELOG](' + changelog_link + ')' if changelog_link.startswith('http') else changelog_link}."
    lines += [versions, ""]
    if not changes:
        lines += ["The API contract did not change (only the schema text or the generator changed).", ""]
    else:
        breaking = sum(1 for c in changes if c.breaking)
        lines += [f"Breaking changes: {breaking if breaking else 'none'}", ""]
        lines += ["| Endpoint | Change | Breaking | Detail | Definition |", "| --- | --- | --- | --- | --- |"]
        for c in changes[:MAX_ROWS]:
            definition = f"{_code(c.old_def or 'none')} to {_code(c.new_def or 'none')}"
            lines.append(
                f"| {_code(c.endpoint)} | {c.kind} | {'**breaking**' if c.breaking else 'additive'} | "
                f"{_code(c.detail)} | {definition} |"
            )
        if len(changes) > MAX_ROWS:
            lines.append("")
            lines.append(f"_and {len(changes) - MAX_ROWS} more change(s): see the generated diff_")
        lines.append("")
    if problems:
        lines += ["### Problems found by the contract check", ""]
        lines += [f"- {_code(problem)}" for problem in problems[:MAX_PROBLEMS]]
        if len(problems) > MAX_PROBLEMS:
            lines.append(f"- _and {len(problems) - MAX_PROBLEMS} more_")
        lines.append("")
    return "\n".join(lines)


def render_json(
    changes: List[Change], problems: List[str], base_api: str, head_api: str, top: str, base_ref: str
) -> str:
    report = {
        "base_ref": base_ref,
        "base_api": base_api,
        "head_api": head_api,
        "top": top,
        "changes": [asdict(change) for change in changes],
        "problems": problems,
    }
    return json.dumps(report, indent=2) + "\n"


def _head_sha() -> Optional[str]:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apiserver.contract.evolution", description=__doc__)
    parser.add_argument("--base-ref", required=True, help="the commit to compare with, for example origin/main")
    parser.add_argument("--head-schema", type=Path, default=Path(__file__).parents[1] / "schema" / "services")
    parser.add_argument("--head-api", help="the API version of the head (default: the one in the code)")
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    parser.add_argument("--markdown-report", type=Path, help="write the change list as a markdown section to this file")
    parser.add_argument("--json-report", type=Path, help="write the change list as JSON to this file")
    parser.add_argument("--head-sha", help="the commit of the head, for the changelog link (default: HEAD)")
    args = parser.parse_args(argv)

    head_api = args.head_api or re.search(
        r'API_VERSION\s*=\s*"([\d.]+)"',
        (Path(__file__).parents[1] / "service_repo" / "api_version.py").read_text(),
    ).group(1)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            base_schema, base_api = _schema_from_ref(args.base_ref, Path(tmp))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        changelog = args.changelog.read_text() if args.changelog.exists() else ""
        try:
            base_endpoints, head_endpoints = load_api(base_schema), load_api(args.head_schema)
        except (ValueError, ConfigException, ParseBaseException) as error:
            print(f"PROBLEM {error}")
            return 1
        changes, problems, top = assess(base_endpoints, head_endpoints, base_api, head_api, changelog)

    print(f"API {base_api} -> {top} (API version {head_api}), compared with {args.base_ref}")
    for c in changes:
        print(f"{'BREAKING' if c.breaking else 'ok      '} {c.kind:8} {c.endpoint}: {c.detail}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    print(f"{len(changes)} change(s), {len(problems)} problem(s)")
    if args.markdown_report:
        link = changelog_link(args.head_sha or _head_sha(), top, changelog)
        args.markdown_report.write_text(
            render_markdown(changes, problems, base_api, top, args.base_ref, head_api, link), encoding="utf-8"
        )
    if args.json_report:
        args.json_report.write_text(
            render_json(changes, problems, base_api, head_api, top, args.base_ref), encoding="utf-8"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
