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
that names the endpoint. Run it from the repository root, against the commit to compare with::

    python -m apiserver.contract.evolution --base-ref origin/main
"""

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pyhocon import ConfigFactory

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
    """Every endpoint of the schema folder, by its full name ``service.action``."""
    api: Dict[str, Endpoint] = {}
    for path in sorted(Path(schema_root).glob("*.conf")):
        if path.name.startswith("_"):
            continue
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
        if before is None:
            changes.append(Change(name, "added", False, f"new endpoint, from API {head_api}"))
            continue
        if after is None:
            changes.append(Change(name, "removed", True, f"endpoint removed at API {head_api}"))
            continue
        if base[name].action != head[name].action:
            changes.append(Change(name, "changed", True, "the settings of the endpoint (access, roles) changed"))
        changes.extend(_block_changes(name, before, after, base[name].definitions, head[name].definitions))
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
        if c.endpoint not in section:
            problems.append(f"{c.endpoint}: {c.detail} is not named in the changelog section 'API {head_api}'")
    return problems


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
    archive = subprocess.run(
        ["git", "archive", ref, "apiserver/schema/services"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive.stdout, check=True)
    for path, pattern in VERSION_SOURCES:
        text = _git_show(ref, path)
        if text is not None and re.search(pattern, text):
            return destination / "apiserver/schema/services", re.search(pattern, text).group(1)
    raise ValueError(f"no API version found in {ref}")


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
    changes = diff(base, head, base_api, top)
    problems += evaluate(changes, base_api, top, changelog)
    return changes, problems, top


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apiserver.contract.evolution", description=__doc__)
    parser.add_argument("--base-ref", required=True, help="the commit to compare with, for example origin/main")
    parser.add_argument("--head-schema", type=Path, default=Path(__file__).parents[1] / "schema" / "services")
    parser.add_argument("--head-api", help="the API version of the head (default: the one in the code)")
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    args = parser.parse_args(argv)

    head_api = args.head_api or re.search(
        r'API_VERSION\s*=\s*"([\d.]+)"',
        (Path(__file__).parents[1] / "service_repo" / "api_version.py").read_text(),
    ).group(1)
    with tempfile.TemporaryDirectory() as tmp:
        base_schema, base_api = _schema_from_ref(args.base_ref, Path(tmp))
        changelog = args.changelog.read_text() if args.changelog.exists() else ""
        changes, problems, top = assess(
            load_api(base_schema), load_api(args.head_schema), base_api, head_api, changelog
        )

    print(f"API {base_api} -> {top} (API version {head_api}), compared with {args.base_ref}")
    for c in changes:
        print(f"{'BREAKING' if c.breaking else 'ok      '} {c.kind:8} {c.endpoint}: {c.detail}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    print(f"{len(changes)} change(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
