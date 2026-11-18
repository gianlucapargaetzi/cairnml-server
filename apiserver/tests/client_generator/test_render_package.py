import ast
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apiserver.client_generator.python_renderer import render_package
from apiserver.client_generator.schema_model import load_schema

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "schema"
SCHEMA = load_schema(FIXTURES)


def test_the_package_file_lists_every_service_sorted_and_names_none_twice():
    text = render_package(["maps", "demo", "enums"], SCHEMA)
    assert '__all__ = [\n    "demo",\n    "enums",\n    "maps",\n]\n' in text


def test_the_package_file_imports_a_service_module_on_first_access():
    text = render_package(["demo"], SCHEMA)
    assert "def __getattr__(name: str) -> Any:" in text
    assert 'importlib.import_module(f"{__name__}.{name}")' in text
    assert "raise AttributeError" in text


def test_the_package_file_is_the_same_on_a_second_run_and_has_no_date_or_path():
    first = render_package(["demo", "maps"], SCHEMA)
    assert first == render_package(["demo", "maps"], SCHEMA)
    assert str(HERE) not in first
    assert "202" not in first


def test_the_package_file_parses_as_python_3_9():
    ast.parse(render_package(["demo"], SCHEMA), feature_version=(3, 9))


def test_the_package_lists_every_retained_definition_oldest_first_and_the_api_level():
    text = render_package(["demo"], SCHEMA)
    assert '"get_all": (\n            ("2.1", "GetAllRequest", "GetAllResponse"),\n' in text
    assert '            ("2.15", "GetAllRequest", "GetAllResponse"),\n' in text
    assert '            ("2.30", "GetAllRequest", "GetAllResponse"),\n' in text
    assert '"get_by_id": (\n            ("2.4", "GetByIdRequest", "GetByIdResponse"),\n' in text
    assert 'API_LEVEL = "2.42"\n' in text  # the highest version of any definition of the schema (enums.conf)


def _generate_package(folder: Path, name: str) -> None:
    """Generates the whole demo schema (as the queues service) into `folder`, as the CLI does."""
    schema = folder.parent / "schema"
    schema.mkdir()
    shutil.copy(FIXTURES / "demo.conf", schema / "queues.conf")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apiserver.client_generator",
            "generate",
            "--target",
            "sdk",
            "--schema",
            str(schema),
            "--out",
            str(folder),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[3],
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def generated(tmp_path, monkeypatch):
    """The generated services package of the demo schema, importable under its own name."""
    pytest.importorskip("cairnml")
    name = "generated_services_213"
    folder = tmp_path / name
    _generate_package(folder, name)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield importlib.import_module(name)
    for module in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[module]


def test_importing_the_package_imports_none_of_its_service_modules(tmp_path, monkeypatch):
    pytest.importorskip("cairnml")
    name = "generated_services_213_lazy"
    _generate_package(tmp_path / name, name)
    monkeypatch.syspath_prepend(str(tmp_path))
    before = set(sys.modules)
    importlib.import_module(name)
    loaded = set(sys.modules) - before
    assert f"{name}.queues" not in loaded
    assert not any(module.startswith(f"{name}.legacy") for module in loaded)


def test_resolve_at_a_version_between_two_definitions_gives_the_older_one(generated):
    request, response = generated.resolve("queues", "get_all", "2.20")
    assert request.__name__ == "GetAllRequest"
    assert request._version == "2.15"
    assert response._version == "2.15"
    assert request.__module__ == f"{generated.__name__}.legacy.v2_15.queues"


def test_resolve_at_the_version_a_definition_began_gives_that_definition(generated):
    assert generated.resolve("queues", "get_all", "2.1")[0]._version == "2.1"
    assert generated.resolve("queues", "get_all", "2.15")[0]._version == "2.15"


def test_resolve_at_or_above_the_newest_gives_the_newest_in_the_service_module(generated):
    for version in ("2.30", "2.31", "2.99"):
        request, response = generated.resolve("queues", "get_all", version)
        assert request._version == "2.30"
        assert request.__module__ == f"{generated.__name__}.queues"
        assert response.__module__ == f"{generated.__name__}.queues"


def test_resolve_below_the_oldest_definition_raises_naming_the_oldest(generated):
    with pytest.raises(generated.UnsupportedApiVersion, match="oldest version it has is 2.1"):
        generated.resolve("queues", "get_all", "2.0")


def test_resolve_of_an_action_with_one_definition(generated):
    assert generated.resolve("queues", "get_by_id", "2.4")[0]._version == "2.4"
    with pytest.raises(generated.UnsupportedApiVersion):
        generated.resolve("queues", "get_by_id", "2.3")


def test_resolve_of_an_unknown_action_is_a_key_error(generated):
    with pytest.raises(KeyError):
        generated.resolve("queues", "nope", "2.4")
