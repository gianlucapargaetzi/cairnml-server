"""An enumeration field accepts a string that is a value of the enumeration (it becomes the member), a member, and a
string that is not a value of it (a newer server may send one; it is kept as the string). A list of enumeration
values behaves the same way per item."""

from pathlib import Path

import pytest

from apiserver.client_generator.python_renderer import render_service
from apiserver.client_generator.schema_model import load_schema

HERE = Path(__file__).parent
SESSION = "cairnml.backend_api.session"


def _text() -> str:
    return render_service(load_schema(HERE / "fixtures" / "schema")["enums"], SESSION)


@pytest.fixture(scope="module")
def generated():
    pytest.importorskip("cairnml")
    namespace = {"__name__": "generated_enums"}
    exec(compile(_text(), "enums.py", "exec"), namespace)
    return namespace


def test_an_enumeration_field_takes_a_value_a_member_or_an_unknown_string(generated):
    mode = generated["ModeEnum"]
    assert generated["SetModeRequest"](mode="fast").mode is mode.fast
    assert generated["SetModeRequest"](mode=mode.slow).mode is mode.slow
    assert generated["SetModeRequest"](mode="unknown-value").mode == "unknown-value"


def test_an_enumeration_field_keeps_what_was_set_when_it_is_set_later(generated):
    mode = generated["ModeEnum"]
    request = generated["SetModeRequest"](mode="fast")
    request.mode = "slow"
    assert request.mode is mode.slow
    request.mode = None
    assert request.mode is None


def test_an_enumeration_field_refuses_a_value_that_is_not_a_string(generated):
    with pytest.raises(Exception, match="mode"):
        generated["SetModeRequest"](mode=5)


def test_a_list_of_enumeration_values_converts_values_and_keeps_unknown_strings(generated):
    mode = generated["ModeEnum"]
    request = generated["SetModeRequest"](mode="fast", modes=["fast", mode.slow, "unknown-value"])
    assert request.modes[0] is mode.fast
    assert request.modes[1] is mode.slow
    assert request.modes[2] == "unknown-value"


def test_the_text_of_an_enumeration_field_converts_and_keeps_unknown_values():
    text = _text()
    assert "value = ModeEnum(value)" in text
    assert "except ValueError:" in text
    assert 'self.assert_isinstance(value, "mode", six.string_types)' not in text


def test_the_text_of_a_list_of_enumeration_values_converts_each_item():
    text = _text()
    assert "for v in value:" in text
    assert "converted.append(v)" in text
    assert 'self.assert_isinstance(value, "modes", six.string_types + (enum.Enum,), is_array=True)' in text
