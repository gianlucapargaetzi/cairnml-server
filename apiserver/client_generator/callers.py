"""
Checks the hand-written callers of the API in Python source against the schema:
- a request class of a generated service (`queues.CreateRequest(...)`) must name an action the schema has;
- a keyword of its constructor must be a field of that action's newest request;
- a call with literal `service=`, `action=` keywords must name an action the schema has, and a literal `version=` must
  be a version the schema declares for it.

The allow file has one line per exception, `exempt <service.action> <reason>` (the reason is mandatory); blank lines and
lines starting with `#` are ignored.
"""
import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set

from .python_renderer import camel
from .schema_model import Service


@dataclass
class Allow:
    exempt: Dict[str, str] = field(default_factory=dict)


@dataclass
class Finding:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def read_allow(path: Path) -> Allow:
    allow = Allow()
    for number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if parts[0] != "exempt":
            raise ValueError(f"{path}:{number}: unknown line type {parts[0]!r}")
        if len(parts) < 3 or not parts[2].strip():
            raise ValueError(f"{path}:{number}: `exempt {parts[1] if len(parts) > 1 else '<action>'}` needs a reason")
        allow.exempt[parts[1]] = parts[2].strip()
    return allow


def _string(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Visitor(ast.NodeVisitor):
    def __init__(self, path, schema, generated, allow, findings):
        self.path, self.schema, self.generated, self.allow, self.findings = path, schema, generated, allow, findings
        self.aliases: Dict[str, str] = {}  # local name -> generated service
        self.requests = {
            service: {camel(a) + "Request": a for a in schema[service].actions} for service in generated if service in schema
        }

    def report(self, node, message):
        self.findings.append(Finding(self.path, node.lineno, message))

    def visit_ImportFrom(self, node):
        if node.module and node.module.split(".")[-1] == "services":
            for alias in node.names:
                if alias.name in self.generated:
                    self.aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_Call(self, node):
        self.generic_visit(node)
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in self.aliases:
            self._request_class(node, self.aliases[func.value.id], func.attr)
        keywords = {k.arg: _string(k.value) for k in node.keywords if k.arg}
        if keywords.get("service") and keywords.get("action"):
            self._named_call(node, keywords["service"], keywords["action"], keywords.get("version"))

    def _request_class(self, node, service, class_name):
        if not class_name.endswith("Request"):
            return
        action = self.requests[service].get(class_name)
        if action is None:
            from re import sub

            snake = sub(r"(?<!^)(?=[A-Z])", "_", class_name[: -len("Request")]).lower()
            if f"{service}.{snake}" not in self.allow.exempt:
                self.report(node, f"{service}.{snake} is not in the schema ({service}.{class_name})")
            return
        known = set((self.schema[service].actions[action].request.get("properties") or {}))
        for keyword in node.keywords:
            if keyword.arg and keyword.arg not in known:
                self.report(node, f"{service}.{action} has no field `{keyword.arg}`")

    def _named_call(self, node, service, action, version):
        full = f"{service}.{action}"
        if full in self.allow.exempt:
            return
        found = self.schema[service].actions.get(action) if service in self.schema else None
        if found is None:
            self.report(node, f"{full} is not in the schema")
        elif version is not None and version not in found.versions:
            self.report(node, f"{version} is not a version of {full} (declared: {', '.join(found.versions)})")


def check_callers(root: Path, schema: Dict[str, Service], generated: Set[str], allow: Allow) -> List[Finding]:
    root = Path(root)
    paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
    findings: List[Finding] = []
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        _Visitor(path, schema, set(generated), allow, findings).visit(tree)
    return findings
