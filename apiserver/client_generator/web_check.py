"""
Checks the web app's hand-edited API layer (TypeScript) against the schema:
- a call `${this.basePath}/v<version>/<service>.<action>` in api-services/*.service.ts must name an action the schema
  has, at the version of that action's definition. The actions of web_renderer.UNVERSIONED_ACTIONS (login.logout, whose
  cookie path has no version, and the identity provider's addresses) are the opposite: called without a version;
- a field of a request or response model (model/<service>/<service><Action>Request|Response.ts) must be a field the
  schema defines for that action: a field the schema lacks is sent to, or read from, a server that does not know it.
  A schema field the web app does not have is not an error (the web app may not use it).

The allow file has one line per exception, `exempt <service.action> <reason>` (the reason is mandatory); it covers the
action's call and both of its models. An exception that no longer matches anything is reported, so the file only
holds real, current drift.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from .callers import Allow, Finding
from .schema_model import Service
from .web_renderer import UNVERSIONED_ACTIONS

CALL = re.compile(r"\$\{this\.basePath\}/(?:v(\d+(?:\.\d+)*)/)?([a-z_]+)\.([a-z_0-9]+)")


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _properties(node, definitions: dict, depth: int = 0) -> Set[str]:
    if depth > 8 or not isinstance(node, dict):
        return set()
    if "$ref" in node:
        return _properties(definitions.get(node["$ref"].split("/")[-1], {}), definitions, depth + 1)
    found = set(node.get("properties") or {})
    for key in ("allOf", "anyOf", "oneOf"):
        for part in node.get(key) or []:
            found |= _properties(part, definitions, depth + 1)
    return found


def interface_fields(text: str, name: str) -> Optional[Set[str]]:
    """The top-level field names of `export interface <name> {...}`, or None when the file has no such interface."""
    match = re.search(r"export interface " + re.escape(name) + r"\b[^{]*\{", text)
    if not match:
        return None
    depth, end = 1, match.end()
    while end < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[end], 0)
        end += 1
    fields: Set[str] = set()
    nesting = 0
    for line in text[match.end() : end - 1].split("\n"):
        if nesting == 0:
            field = re.match(r"\s+(\w+)\??:", line)
            if field:
                fields.add(field.group(1))
        nesting += line.count("{") - line.count("}")
    return fields


def check_web(root: Path, schema: Dict[str, Service], allow: Allow) -> List[Finding]:
    """root is the web app's business-logic folder (it holds api-services/ and model/)."""
    root = Path(root)
    findings: List[Finding] = []
    used: Set[str] = set()

    for path in sorted((root / "api-services").glob("*.service.ts")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for version, service, action in CALL.findall(line):
                full = f"{service}.{action}"
                if service in schema and action in schema[service].actions:
                    defined = schema[service].actions[action].version
                    if full in UNVERSIONED_ACTIONS:
                        if version:
                            findings.append(
                                Finding(
                                    path,
                                    number,
                                    f"{full} is sent at version {version}: it is called without a version ({UNVERSIONED_ACTIONS[full]})",
                                )
                            )
                    elif not version:
                        findings.append(
                            Finding(
                                path,
                                number,
                                f"{full} is sent without a version, its definition is {defined}: /v<version>/<service>.<action>",
                            )
                        )
                    elif version != defined:
                        findings.append(
                            Finding(path, number, f"{full} is sent at version {version}, its definition is {defined}")
                        )
                    continue
                if full in allow.exempt:
                    used.add(full)
                else:
                    findings.append(Finding(path, number, f"{full} is not an action of the server's schema"))

    for service in schema.values():
        for action in service.actions.values():
            for kind, node in (("Request", action.request), ("Response", action.response)):
                if not node:
                    continue
                name = f"{service.name}{_pascal(action.name)}{kind}"
                path = root / "model" / service.name / f"{name}.ts"
                path = path.with_name(name[0].lower() + name[1:] + ".ts")
                if not path.is_file():
                    continue
                fields = interface_fields(path.read_text(), name[0].upper() + name[1:])
                extra = sorted((fields or set()) - _properties(node, service.definitions))
                if not extra:
                    continue
                if action.full_name in allow.exempt:
                    used.add(action.full_name)
                else:
                    findings.append(
                        Finding(path, 1, f"{action.full_name} {kind.lower()}: {extra} are not in the schema")
                    )

    for key in sorted(set(allow.exempt) - used):
        findings.append(Finding(Path("allow file"), 1, f"exempt {key} no longer matches anything: remove it"))
    return findings
