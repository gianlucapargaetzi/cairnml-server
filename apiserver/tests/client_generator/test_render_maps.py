"""A map of definitions converts its values: a definition that is an object whose values all refer to one class becomes
a dict class that converts each value (the field that refers to it gets the same conversion), and a field that is a
map of such maps converts its values the same way. Other maps (any value, a simple value, a value that is not an
object) stay plain dictionaries."""

import json
from pathlib import Path

import pytest

from apiserver.client_generator.python_renderer import render_service
from apiserver.client_generator.schema_model import load_schema

HERE = Path(__file__).parent
SESSION = "cairnml.backend_api.session"


def _text() -> str:
    return render_service(load_schema(HERE / "fixtures" / "schema")["maps"], SESSION)


@pytest.fixture(scope="module")
def generated():
    pytest.importorskip("cairnml")
    namespace = {"__name__": "generated_maps"}
    exec(compile(_text(), "maps.py", "exec"), namespace)
    return namespace


def test_a_map_definition_is_a_dict_class_of_the_item_class(generated):
    section = generated["ParamSection"]
    assert issubclass(section, dict)
    assert issubclass(section, generated["NonStrictDataModel"])
    value = section({"a": {"name": "lr", "value": "0.1"}})
    assert type(value["a"]) is generated["ParamItem"]
    assert value["a"].value == "0.1"


def test_a_field_that_refers_to_a_map_definition_converts_its_values(generated):
    request = generated["SetParamsRequest"](section={"a": {"name": "lr", "value": "0.1"}})
    assert type(request.section) is generated["ParamSection"]
    assert type(request.section["a"]) is generated["ParamItem"]


def test_a_map_of_maps_converts_the_values_of_each_map(generated):
    request = generated["SetParamsRequest"](section={}, sections={"General": {"a": {"name": "lr", "value": "0.1"}}})
    assert type(request.sections) is dict
    assert type(request.sections["General"]) is generated["ParamSection"]
    assert type(request.sections["General"]["a"]) is generated["ParamItem"]


def test_a_converted_value_can_be_given_already_converted(generated):
    item = generated["ParamItem"](name="lr", value="0.1")
    request = generated["SetParamsRequest"](section={"a": item})
    assert request.section["a"] is item


def test_a_map_value_that_is_not_an_item_is_refused(generated):
    with pytest.raises(TypeError, match="param_section"):
        generated["SetParamsRequest"](section={"a": 5})


def test_a_map_of_maps_refuses_a_value_that_is_not_a_map(generated):
    with pytest.raises(TypeError, match="sections"):
        generated["SetParamsRequest"](section={}, sections={"General": 5})


def test_a_map_serialises_to_plain_json_with_its_items(generated):
    request = generated["SetParamsRequest"](
        section={"a": {"name": "lr", "value": "0.1"}}, sections={"General": {"a": {"value": "1"}}}
    )
    plain = request.to_dict()
    assert plain["section"] == {"a": {"name": "lr", "value": "0.1"}}
    assert json.loads(json.dumps(plain))["sections"] == {"General": {"a": {"value": "1"}}}
    assert type(generated["ParamSection"]({"a": {"value": "1"}}).to_dict()) is dict


def test_a_response_model_converts_a_map_of_maps(generated):
    response = generated["SetParamsResponse"](sections={"General": {"a": {"name": "lr"}}})
    assert type(response.sections["General"]) is generated["ParamSection"]
    assert type(response.sections["General"]["a"]) is generated["ParamItem"]


def test_maps_that_are_not_maps_of_definitions_stay_dictionaries(generated):
    request = generated["SetParamsRequest"](
        section={},
        labels={"k": "v"},
        extra={"anything": [1, 2]},
        modes={"lr": "fast"},
    )
    for name in ("labels", "extra", "modes"):
        assert type(getattr(request, name)) is dict, name
    assert request.modes == {"lr": "fast"}
    assert request.extra == {"anything": [1, 2]}


def test_the_text_of_a_map_definition_converts_each_value_to_its_item_class():
    text = _text()
    assert "class ParamSection(dict, NonStrictDataModel):" in text
    assert "ParamItem.from_dict(v) if isinstance(v, dict) else v" in text
    assert 'self.assert_isinstance(converted.values(), "param_section", (ParamItem,), is_array=True)' in text


def test_the_text_of_a_field_that_refers_to_a_map_definition_converts_the_value():
    text = _text()
    assert "value = ParamSection(value)" in text


def test_the_text_of_a_map_of_maps_converts_each_value_to_the_map_class():
    text = _text()
    assert "value = {k: ParamSection(v) if isinstance(v, dict) else v for k, v in value.items()}" in text
    assert 'self.assert_isinstance(value.values(), "sections", (ParamSection,), is_array=True)' in text


def test_the_text_of_a_map_that_stays_a_dictionary_has_no_conversion():
    text = _text()
    assert "class LabelsMap" not in text
    assert "class ExtraMap" not in text
    assert 'self.assert_isinstance(value, "labels", (dict,))' in text
    assert 'self.assert_isinstance(value, "extra", (dict,))' in text


def test_the_task_hyperparams_of_the_schema_are_typed():
    schema = load_schema(HERE.parents[1] / "schema" / "services")
    text = render_service(schema["tasks"], SESSION)
    assert "class SectionParams(dict, NonStrictDataModel):" in text
    assert 'self.assert_isinstance(value.values(), "hyperparams", (SectionParams,), is_array=True)' in text
