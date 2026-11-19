"""
Checks the web app's hand-edited API layer (TypeScript) against the schema:
- a call `${this.basePath}/v<version>/<service>.<action>` in api-services/*.service.ts must name an action the schema
  has, at the version of that action's definition. The actions of web_renderer.UNVERSIONED_ACTIONS (login.logout, whose
  cookie path has no version, and the identity provider's addresses) are the opposite: called without a version;
- a hand-written source (`*.ts` under `sources`, not a `*.spec.ts`, not a file of the generated `api-services/` and
  `model/` folders of `root`) writes a request path by hand, `/<service>.<action>` (optionally behind
  `/v<major>.<minor>/`), for an action of the schema: the generated function of that action is called instead
  (`ApiTasksService.tasksEdit`). A `/v<major>.<minor>/` literal in such a source is an API version written by hand,
  and the address comes from the generated functions. An action named as a string in `endpointUrl('<service>.<action>')`
  is not a path, but the same rule applies: the generated function is called instead. The unversioned actions of
  UNVERSIONED_ACTIONS (the login addresses) are exempt from that call;
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
# a request path written by hand: `/<service>.<action>`, optionally behind `/v<major>.<minor>/`, not part of a longer name.
# A bare action name ('user_access.check', or the endpointUrl('login.idp_start') of the generated address) is not a path.
PATH_LITERAL = re.compile(r"/(?:v\d+\.\d+/)?([a-z_]+)\.([a-z_0-9]+)(?![\w.])")
# an action named as a string for the generated address: endpointUrl('access.share')
ENDPOINT_CALL = re.compile(r"endpointUrl\(\s*['\"`]([a-z_]+)\.([a-z_0-9]+)['\"`]\s*\)")
VERSION_LITERAL = re.compile(r"/v\d+\.\d+/")
GENERATED_FOLDERS = ("api-services", "model")


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _generated_function(service: str, action: str) -> str:
    """The generated function of an action: ApiTasksService.tasksEdit (web_renderer.render_api)."""
    return f"Api{_pascal(service)}Service.{service}{_pascal(action)}"


def hand_written_sources(root: Path, sources: Path) -> List[Path]:
    """The .ts files of the sources that are hand-written: no spec file, and no file of the generated folders of root."""
    root = Path(root).resolve()
    found = []
    for path in sorted(Path(sources).resolve().rglob("*.ts")):
        if path.name.endswith(".spec.ts"):
            continue
        if path.is_relative_to(root) and path.relative_to(root).parts[0] in GENERATED_FOLDERS:
            continue
        found.append(path)
    return found


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


def check_web(root: Path, schema: Dict[str, Service], allow: Allow, sources: Optional[Path] = None) -> List[Finding]:
    """root is the web app's business-logic folder (it holds api-services/ and model/). sources is the web app's src
    folder, whose hand-written code is checked for calls written by hand; None checks the generated layer only."""
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

    for path in hand_written_sources(root, sources) if sources is not None else []:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for service, action in PATH_LITERAL.findall(line):
                full = f"{service}.{action}"
                if service not in schema or action not in schema[service].actions:
                    continue
                if full in allow.exempt:
                    used.add(full)
                    continue
                findings.append(
                    Finding(
                        path,
                        number,
                        f"{full} is written as a string in hand-written code: call "
                        f"{_generated_function(service, action)} (the generated function) instead",
                    )
                )
            for service, action in ENDPOINT_CALL.findall(line):
                full = f"{service}.{action}"
                if full in UNVERSIONED_ACTIONS or service not in schema or action not in schema[service].actions:
                    continue
                if full in allow.exempt:
                    used.add(full)
                    continue
                findings.append(
                    Finding(
                        path,
                        number,
                        f"{full} is named by endpointUrl in hand-written code: call "
                        f"{_generated_function(service, action)} (the generated function) instead",
                    )
                )
            for literal in VERSION_LITERAL.finditer(line):
                findings.append(
                    Finding(
                        path,
                        number,
                        f"{literal.group(0)!r} is an API version written in hand-written code: take the address from "
                        "the generated functions (endpointUrl) instead",
                    )
                )

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
