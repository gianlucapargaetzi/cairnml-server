import ast
from pathlib import Path

from apiserver.client_generator.python_renderer import render_service
from apiserver.client_generator.schema_model import load_schema

HERE = Path(__file__).parent
SESSION = "cairnml.backend_api.session"


def _render():
    return render_service(load_schema(HERE / "fixtures" / "schema")["demo"], SESSION)


def test_renders_the_fixture_exactly():
    assert _render() == (HERE / "golden" / "demo.py").read_text()


def test_output_is_identical_on_a_second_run_and_has_no_date_or_path():
    first, second = _render(), _render()
    assert first == second
    assert str(HERE) not in first
    assert "202" not in first


def test_output_parses_as_python_3_9():
    ast.parse(_render(), feature_version=(3, 9))


def test_required_fields_come_first_and_optional_ones_default_to_none():
    text = _render()
    assert "def __init__(self, queue: str, max_task_entries: Optional[int] = None, **kwargs: Any)" in text
