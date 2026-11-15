"""
A standalone reader of the API schema for the generator.

It uses pyhocon only: importing apiserver.schema pulls in the whole server configuration. The test
test_model_lists_the_same_actions_and_versions_as_the_server_reader keeps this reader equal to SchemaReader.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Tuple

from pyhocon import ConfigFactory

VERSION_RE = re.compile(r"^\d+\.\d+$")
DEFINITIONS_PREFIX = "#/definitions/"


def version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def ref_name(ref: str) -> str:
    if not ref.startswith(DEFINITIONS_PREFIX):
        raise ValueError(f"unsupported reference {ref!r}")
    return ref[len(DEFINITIONS_PREFIX):]


@dataclass(frozen=True)
class Action:
    """One action, as its newest definition; `versions` lists every version the schema declares."""

    service: str
    name: str
    version: str
    versions: Tuple[str, ...]
    description: str
    request: dict  # empty for an action that has a batch_request instead (not generated yet)
    response: dict

    @property
    def full_name(self) -> str:
        return f"{self.service}.{self.name}"


@dataclass(frozen=True)
class Service:
    name: str
    description: str
    definitions: dict
    actions: Dict[str, Action]


def _load_service(path: Path) -> Service:
    conf = ConfigFactory.parse_file(str(path)).as_plain_ordered_dict()
    name = path.stem
    actions = {}
    for action_name, group in conf.items():
        if action_name.startswith("_"):
            continue
        versions = sorted((key for key in group if VERSION_RE.match(key)), key=version_key)
        if not versions:
            raise ValueError(f"{name}.{action_name} declares no version")
        version = versions[-1]
        newest = group[version]
        actions[action_name] = Action(
            service=name,
            name=action_name,
            version=version,
            versions=tuple(versions),
            description=newest.get("description", ""),
            request=newest.get("request") or {},
            response=newest.get("response") or {},
        )
    return Service(
        name=name,
        description=conf.get("_description", ""),
        definitions=conf.get("_definitions") or {},
        actions=actions,
    )


def load_schema(root: Path) -> Dict[str, Service]:
    """Every service of the schema folder (files `*.conf` that do not start with an underscore)."""
    root = Path(root)
    return {
        path.stem: _load_service(path)
        for path in sorted(root.glob("*.conf"))
        if not path.name.startswith("_")
    }


def _refs(node, found: list) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(ref_name(value))
            else:
                _refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _refs(item, found)


def reachable_definitions(fragments: Iterable[dict], definitions: dict) -> dict:
    """The definitions that the schema fragments use, directly or through other definitions (service order)."""
    seen = set()
    pending = []
    for fragment in fragments:
        _refs(fragment, pending)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        if name not in definitions:
            raise ValueError(f"reference to the unknown definition {name!r}")
        seen.add(name)
        _refs(definitions[name], pending)
    return {name: value for name, value in definitions.items() if name in seen}
