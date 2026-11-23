"""Finds where the API the server defines and the API its callers use disagree.

Four sources are compared:
- the schema (``apiserver/schema/services``, read with the client generator's loader): every action and its request fields;
- the endpoints (``@endpoint("service.action")`` in ``apiserver/services``);
- the generated clients of the SDK and the agent (classes with ``_service``, ``_action`` and ``_schema``);
- the raw calls: ``service.action`` strings in the SDK, the agent and the web app's generated services.

A problem is reported unless the allow-list names it with a reason. An allow-list entry that no longer matches anything is a
problem too, so the list cannot go stale. The allow-list line format is ``<area> <kind> <service.action> <reason>``.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from apiserver.client_generator.schema_model import Service, load_schema

SCHEMA_ROOT = Path(__file__).parents[1] / "schema" / "services"
SERVICES_ROOT = Path(__file__).parents[1] / "services"

ENDPOINT = re.compile(r"""^\s*@endpoint\(\s*["']([a-z_.0-9]+)["']""", re.M)
CALL_SKIP = (
    "/node_modules",
    "/_vendor",
    "backend_api/services",
    "/__pycache__",
    "/business-logic/model",
    "/.git",
    "/.venv",
    "/tests",
)
# Calls to these services are looked for even where the server has none (they are the callers' own API).
CALLER_ONLY_SERVICES = ("datasets", "dataviews", "frames", "storage")


@dataclass(frozen=True)
class Problem:
    area: str  # "server", "sdk", "agent" or "web"
    kind: str
    name: str  # service.action (or service.action.field)
    where: str  # files or the reason it was found, for the report

    @property
    def key(self) -> str:
        return f"{self.area} {self.kind} {self.name}"

    def __str__(self) -> str:
        return f"{self.key}  ({self.where})"


def schema_actions(root: Path = SCHEMA_ROOT) -> Dict[str, Service]:
    return load_schema(root)


def implemented_actions(root: Path = SERVICES_ROOT) -> Dict[str, Set[str]]:
    found: Dict[str, Set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        for match in ENDPOINT.finditer(path.read_text(encoding="utf-8")):
            service, _, action = match.group(1).partition(".")
            found.setdefault(service, set()).add(action)
    return found


def generated_client_actions(root: Path) -> Dict[str, Dict[str, Set[str]]]:
    """service -> action -> request fields, from the generated client modules under ``root`` (``v2_*/*.py``)."""
    found: Dict[str, Dict[str, Set[str]]] = {}
    for path in sorted(root.glob("v2_*/*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            values = {}
            for statement in node.body:
                if (
                    isinstance(statement, ast.Assign)
                    and isinstance(statement.targets[0], ast.Name)
                    and statement.targets[0].id in ("_service", "_action", "_schema")
                ):
                    try:
                        values[statement.targets[0].id] = ast.literal_eval(statement.value)
                    except ValueError:
                        values[statement.targets[0].id] = None
            if "_service" not in values or "_action" not in values:
                continue
            fields = (
                set((values.get("_schema") or {}).get("properties", {})) if node.name.endswith("Request") else set()
            )
            found.setdefault(values["_service"], {}).setdefault(values["_action"], set()).update(fields)
    return found


def raw_calls(
    root: Path, services: Iterable[str], suffixes: Tuple[str, ...], only_in: Optional[str] = None
) -> Dict[Tuple[str, str], Set[str]]:
    """(service, action) -> the files that name it as a string, for the given services."""
    pattern = re.compile(
        r"""(?:["'`/]|basePath\}/|API_BASE_URL\}/)(%s)\.([a-z_]+)["'`?(\s]""" % "|".join(sorted(services))
    )
    found: Dict[Tuple[str, str], Set[str]] = {}
    for path in sorted(root.rglob("*")):
        text_path = path.as_posix()
        if not path.is_file() or not path.name.endswith(suffixes) or path.name.endswith(".spec.ts"):
            continue
        if any(skip in text_path for skip in CALL_SKIP) or (only_in and only_in not in text_path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in pattern.finditer(text):
            found.setdefault((match.group(1), match.group(2)), set()).add(str(path.relative_to(root)))
    return found


def server_problems(schema: Dict[str, Service], implemented: Dict[str, Set[str]]) -> List[Problem]:
    problems = []
    for service in sorted(schema):
        for action in sorted(set(schema[service].actions) - implemented.get(service, set())):
            problems.append(Problem("server", "schema action without an endpoint", f"{service}.{action}", "schema"))
    for service in sorted(implemented):
        for action in sorted(implemented[service] - set(schema.get(service, Service("", "", {}, {})).actions)):
            problems.append(Problem("server", "endpoint without a schema action", f"{service}.{action}", "endpoints"))
    return problems


def client_problems(
    area: str, clients: Dict[str, Dict[str, Set[str]]], schema: Dict[str, Service], where: str
) -> List[Problem]:
    problems = []
    for service in sorted(clients):
        if service not in schema:
            problems.append(Problem(area, "generated client for a service the server lacks", service, where))
            continue
        for action, fields in sorted(clients[service].items()):
            declared = schema[service].actions.get(action)
            if declared is None:
                problems.append(Problem(area, "generated client action the server lacks", f"{service}.{action}", where))
                continue
            for field in sorted(fields - set(declared.request.get("properties", {}))):
                problems.append(
                    Problem(area, "generated client field the schema lacks", f"{service}.{action}.{field}", where)
                )
    return problems


def call_problems(area: str, calls: Dict[Tuple[str, str], Set[str]], schema: Dict[str, Service]) -> List[Problem]:
    problems = []
    for (service, action), files in sorted(calls.items()):
        if service not in schema or action not in schema[service].actions:
            problems.append(
                Problem(
                    area,
                    "raw call to an action the server lacks",
                    f"{service}.{action}",
                    ", ".join(sorted(files))[:300],
                )
            )
    return problems


def read_allow(path: Optional[Path]) -> Dict[str, str]:
    """key -> reason. A line is ``<area> <kind words...> <service.action> <reason>``; the kind is the words up to the name."""
    allowed: Dict[str, str] = {}
    if path is None or not path.exists():
        return allowed
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            raise ValueError(f"{path}:{number}: an entry needs a reason after the action")
        reason = parts[1]
        tokens = line.split()
        name_index = next((i for i, token in enumerate(tokens) if "." in token and i > 0), None)
        if name_index is None or not reason.strip():
            raise ValueError(f"{path}:{number}: an entry is '<area> <kind> <service.action> <reason>'")
        key = " ".join(tokens[: name_index + 1])
        reason_text = " ".join(tokens[name_index + 1 :])
        if not reason_text:
            raise ValueError(f"{path}:{number}: {key} needs a reason")
        allowed[key] = reason_text
    return allowed


def find_problems(
    schema: Dict[str, Service],
    implemented: Dict[str, Set[str]],
    sdk_clients,
    agent_clients,
    sdk_calls,
    agent_calls,
    web_calls,
) -> List[Problem]:
    problems = server_problems(schema, implemented)
    problems += client_problems("sdk", sdk_clients, schema, "the SDK's generated client")
    problems += client_problems("agent", agent_clients, schema, "the agent's generated client")
    problems += call_problems("sdk", sdk_calls, schema)
    problems += call_problems("agent", agent_calls, schema)
    problems += call_problems("web", web_calls, schema)
    return problems


def report(problems: List[Problem], allowed: Dict[str, str]) -> Tuple[List[Problem], List[str]]:
    """(problems not on the allow-list, allow-list keys that match nothing)"""
    keys = {problem.key for problem in problems}
    unallowed = [problem for problem in problems if problem.key not in allowed]
    stale = sorted(key for key in allowed if key not in keys)
    return unallowed, stale


def check_workspace(
    workspace: Path, server: Path, allow: Optional[Path], schema_root: Path = SCHEMA_ROOT
) -> Tuple[List[Problem], List[str], int]:
    schema = schema_actions(schema_root)
    implemented = implemented_actions(server / "apiserver" / "services")
    sdk = workspace / "cairnml" / "cairnml"
    agent = workspace / "cairnml" / "cairnml_agent"
    web = workspace / "cairnml-web" / "src"
    services = set(schema) | set(CALLER_ONLY_SERVICES)
    problems = find_problems(
        schema,
        implemented,
        generated_client_actions(sdk / "backend_api" / "services"),
        generated_client_actions(agent / "backend_api" / "services"),
        raw_calls(sdk, services, (".py",)),
        raw_calls(agent, services, (".py",)),
        raw_calls(web, services, (".ts",), only_in="api-services"),
    )
    allowed = read_allow(allow)
    unallowed, stale = report(problems, allowed)
    return unallowed, stale, len(problems) - len(unallowed)
