import importlib
import sys
from pathlib import Path

import pytest

from apiserver.client_generator.python_renderer import render_package

HERE = Path(__file__).parent


def test_the_package_file_lists_every_service_sorted_and_names_none_twice():
    text = render_package(["tasks", "queues", "auth"])
    assert '__all__ = [\n    "auth",\n    "queues",\n    "tasks",\n]\n' in text


def test_the_package_file_imports_a_service_module_on_first_access():
    text = render_package(["queues"])
    assert "def __getattr__(name: str) -> Any:" in text
    assert 'importlib.import_module(f"{__name__}.{name}")' in text
    assert "raise AttributeError" in text


def test_the_package_file_is_the_same_on_a_second_run_and_has_no_date_or_path():
    first = render_package(["queues", "tasks"])
    assert first == render_package(["queues", "tasks"])
    assert str(HERE) not in first
    assert "202" not in first


def test_the_package_file_parses_as_python_3_9():
    import ast

    ast.parse(render_package(["queues"]), feature_version=(3, 9))


@pytest.fixture
def generated_package(tmp_path, monkeypatch):
    """A package folder with the generated `__init__.py` and two stub service modules that record their import."""
    name = "generated_services_206"
    folder = tmp_path / name
    folder.mkdir()
    (folder / "__init__.py").write_text(render_package(["alpha", "beta"]))
    for service in ("alpha", "beta"):
        (folder / f"{service}.py").write_text(f"NAME = {service!r}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    for module in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[module]


def test_importing_the_package_imports_none_of_its_service_modules(generated_package):
    before = set(sys.modules)
    importlib.import_module(generated_package)
    loaded = set(sys.modules) - before
    assert f"{generated_package}.alpha" not in loaded
    assert f"{generated_package}.beta" not in loaded


def test_each_name_of_all_resolves_to_its_service_module_on_first_access(generated_package):
    package = importlib.import_module(generated_package)
    assert package.__all__ == ["alpha", "beta"]
    for service in package.__all__:
        module = getattr(package, service)
        assert module.__name__ == f"{generated_package}.{service}"
        assert module.NAME == service
        assert f"{generated_package}.{service}" in sys.modules


def test_a_name_that_is_not_a_service_is_an_attribute_error(generated_package):
    package = importlib.import_module(generated_package)
    with pytest.raises(AttributeError):
        package.gamma
