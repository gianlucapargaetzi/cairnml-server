"""
Schema and handlers agree, read from the source with `ast` (no service is imported, so no database or Elasticsearch
is needed): every schema action of a generated service has a handler, every handler has a schema action, and no
handler starts above the newest schema version of its action, so a request at the newest version (which is what a
generated client sends) reaches the top handler.
"""

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from apiserver.client_generator.schema_model import load_schema, version_key
from apiserver.client_generator.targets import TARGETS

ROOT = Path(__file__).parents[2]  # apiserver/
SCHEMA = load_schema(ROOT / "schema" / "services")
GENERATED = sorted({s for t in TARGETS.values() for s in t.services})


def handlers(services_dir=ROOT / "services"):
    """Map "service.action" -> list of min_version strings, from the @endpoint decorators."""
    found = defaultdict(list)
    for path in sorted(services_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "endpoint" and node.args):
                continue
            assert isinstance(node.args[0], ast.Constant), f"{path}:{node.lineno}: the endpoint name is not a literal"
            min_version = "1.0"
            for keyword in node.keywords:
                if keyword.arg == "min_version":
                    assert isinstance(keyword.value, ast.Constant), (
                        f"{path}:{node.lineno}: min_version is not a literal"
                    )
                    min_version = keyword.value.value
            found[node.args[0].value].append(min_version)
    return found


HANDLERS = handlers()
ACTIONS = [a.full_name for s in GENERATED for a in SCHEMA[s].actions.values()]


@pytest.mark.parametrize("service", GENERATED)
def test_every_schema_action_has_a_handler(service):
    missing = [a.full_name for a in SCHEMA[service].actions.values() if a.full_name not in HANDLERS]
    assert not missing, f"schema actions without a handler: {missing}"


@pytest.mark.parametrize("service", GENERATED)
def test_every_handler_has_a_schema_action(service):
    names = {a.full_name for a in SCHEMA[service].actions.values()}
    extra = [name for name in HANDLERS if name.startswith(service + ".") and name not in names]
    assert not extra, f"handlers without a schema action: {extra}"


@pytest.mark.parametrize("full_name", ACTIONS)
def test_every_generated_action_has_a_handler_that_serves_its_newest_version(full_name):
    service, action = full_name.split(".")
    newest = SCHEMA[service].actions[action].version
    versions = HANDLERS.get(full_name)
    assert versions, f"{full_name} has no handler"
    assert max(map(version_key, versions)) <= version_key(newest), (
        f"{full_name}: a handler starts at {max(versions, key=version_key)}, above the newest schema version {newest}"
    )
