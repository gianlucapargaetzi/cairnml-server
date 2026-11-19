"""The key escaping of stored dicts and the tag prefix check, used by the business layer.

These helpers are pinned as they behave today. They live in neutral modules so that the business layer does not
depend on the service layer. Characterization only: no behaviour is changed by moving them.
"""

import pytest

from apiserver.apierrors import errors
from apiserver.bll.util import validate_tags
from apiserver.utilities.parameter_key_escaper import (
    escape_dict,
    unescape_dict,
    escape_dict_field,
    unescape_dict_field,
)


@pytest.mark.parametrize("data", [None, {}])
def test_escape_dict_returns_empty_input_as_is(data):
    assert escape_dict(data) is data
    assert unescape_dict(data) is data


def test_escape_dict_escapes_dots_dollars_and_double_underscores():
    assert escape_dict({"a.b": 1, "$x": 2, "__y": 3, "_z": 4, "p%q": 5}) == {
        "a%2Eb": 1,
        "%24x": 2,
        "%_%_y": 3,
        "%_z": 4,
        "p%%q": 5,
    }


def test_unescape_dict_reverses_escape_dict():
    data = {"a.b": 1, "$x": 2, "__y": 3, "_z": 4, "p%q": 5}
    assert unescape_dict(escape_dict(data)) == data


def test_escape_dict_field_escapes_the_nested_dict_at_a_string_path():
    fields = {"model_labels": {"a.b": 1}, "other": {"x.y": 2}}
    escape_dict_field(fields, "model_labels")
    assert fields == {"model_labels": {"a%2Eb": 1}, "other": {"x.y": 2}}


def test_escape_dict_field_escapes_the_nested_dict_at_a_sequence_path():
    fields = {"a": {"b": {"c.d": 1}}}
    escape_dict_field(fields, ("a", "b"))
    assert fields == {"a": {"b": {"c%2Ed": 1}}}


def test_unescape_dict_field_unescapes_the_nested_dict_at_a_list_path():
    fields = {"a": {"b": {"c%2Ed": 1}}}
    unescape_dict_field(fields, ["a", "b"])
    assert fields == {"a": {"b": {"c.d": 1}}}


@pytest.mark.parametrize("fields,path", [({"a": "not a dict"}, "a"), ({}, "missing")])
def test_escape_dict_field_leaves_missing_or_non_dict_values_alone(fields, path):
    before = dict(fields)
    escape_dict_field(fields, path)
    assert fields == before


@pytest.mark.parametrize(
    "tags,system_tags",
    [
        (["ok"], ["sys"]),
        (None, None),
        ([], []),
        (["$x"], []),
        (["__y"], []),
        ([], ["_ok"]),
    ],
)
def test_validate_tags_accepts_plain_tags(tags, system_tags):
    validate_tags(tags, system_tags)


@pytest.mark.parametrize(
    "tags,system_tags",
    [
        (["__$not"], None),
        (["ok", "__$in"], []),
        (None, ["__$nin"]),
    ],
)
def test_validate_tags_rejects_the_list_operator_prefix(tags, system_tags):
    with pytest.raises(errors.bad_request.FieldsValueError):
        validate_tags(tags, system_tags)
