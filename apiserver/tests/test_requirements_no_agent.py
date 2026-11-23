"""The server does not depend on the agent: it has no cairnml-agent requirement and imports no cairnml_agent code."""

import ast
from pathlib import Path

APISERVER = Path(__file__).parents[1]
REQUIREMENTS = APISERVER / "requirements.txt"


def test_the_server_requirements_name_no_agent_package():
    lines = [line.strip() for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()]
    assert not [line for line in lines if line.startswith("cairnml-agent") or line.startswith("cairnml_agent")]


def test_no_module_of_the_server_imports_the_agent():
    offenders = []
    for path in APISERVER.rglob("*.py"):
        if "tests" in path.relative_to(APISERVER).parts[:1]:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] == "cairnml_agent" for name in names):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == []
