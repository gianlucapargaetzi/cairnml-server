from pathlib import Path

import pytest

from apiserver.client_generator.callers import Allow, check_callers, read_allow
from apiserver.client_generator.schema_model import load_schema

HERE = Path(__file__).parent
CALLERS = HERE / "fixtures" / "callers"
SCHEMA = load_schema(HERE / "fixtures" / "schema")


def _check(name, allow=None):
    findings = check_callers(CALLERS / name, SCHEMA, generated={"demo"}, allow=allow or Allow())
    return [f.message for f in findings]


def test_an_action_the_schema_lacks_is_reported():
    assert _check("unknown_action.py") == ["demo.create is not in the schema (demo.CreateRequest)"]


def test_a_field_the_request_does_not_have_is_reported():
    assert _check("unknown_field.py") == ["demo.get_all has no field `onlyfields`"]


def test_calls_by_name_and_literal_versions_are_checked():
    assert _check("unknown_call.py") == [
        "demo.nope is not in the schema",
        "2.5 is not a version of demo.get_all (declared: 2.1, 2.15, 2.30)",
    ]


def test_a_correct_file_is_clean():
    assert _check("clean.py") == []


def test_an_exempt_action_is_not_reported(tmp_path):
    assert _check("async_result.py") == ["async.result is not in the schema"]
    allow_file = tmp_path / "allow.txt"
    allow_file.write_text("# comment\nexempt async.result a real server pseudo-endpoint that is not in the schema\n")
    assert _check("async_result.py", read_allow(allow_file)) == []


def test_an_allow_line_needs_a_reason(tmp_path):
    allow_file = tmp_path / "allow.txt"
    allow_file.write_text("exempt async.result\n")
    with pytest.raises(ValueError, match="reason"):
        read_allow(allow_file)
