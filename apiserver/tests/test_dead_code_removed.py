"""Dead code stays removed: definitions that nothing referenced were deleted, and nothing may bring them back.

The checks read the source with `ast`, so they need no database and run in the unit job."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# (module path below apiserver/, name defined as a function or class anywhere in the module)
REMOVED = [
]


def defined_names(module: str) -> set:
    tree = ast.parse((ROOT / module).read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


@pytest.mark.parametrize("module,name", REMOVED)
def test_removed_definition_is_gone(module, name):
    assert name not in defined_names(module), f"{name} is still defined in {module}"
