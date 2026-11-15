"""Dead code stays removed: definitions that nothing referenced were deleted, and nothing may bring them back.

The checks read the source with `ast`, so they need no database and run in the unit job."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# (module path below apiserver/, name defined as a function or class anywhere in the module)
REMOVED = [
    ("bll/event/scalar_key.py", "ISOTimeKey"),
    ("bll/event/scalar_key.py", "IterKey"),
    ("bll/event/scalar_key.py", "TimestampKey"),
    ("bll/datachunks.py", "delete_objects"),
    ("bll/queue/queue_bll.py", "delete"),
]


def defined_names(module: str) -> set:
    tree = ast.parse((ROOT / module).read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


@pytest.mark.parametrize("module,name", REMOVED)
def test_removed_definition_is_gone(module, name):
    assert name not in defined_names(module), f"{name} is still defined in {module}"


def test_queues_delete_ends_at_its_refusal():
    tree = ast.parse((ROOT / "services/queues.py").read_text())
    (delete,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "delete"]
    assert isinstance(delete.body[-1], ast.Raise), "statements after the raise in queues.delete"


def test_the_old_git_credential_model_and_its_migration_are_gone():
    assert not (ROOT / "database/model/git_credential.py").exists(), "the GitCredential model file still exists"
    assert "migrate_from_git_credentials" not in defined_names("idp/git_credentials.py")
    assert "migrate_from_git_credentials" not in (ROOT / "server_init/app_sequence.py").read_text()
