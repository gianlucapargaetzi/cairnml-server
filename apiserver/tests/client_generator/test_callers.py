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
        "'2.5' is an API version written in hand-written code: take it from the generated classes",
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


def test_a_version_written_in_hand_written_code_is_reported():
    assert _check("version_literals.py") == [
        "'/v2.8/' is an API version written in hand-written code: take it from the generated classes",
        "'2.40' is an API version written in hand-written code: take it from the generated classes",
        "'2.15' is an API version written in hand-written code: take it from the generated classes",
    ]


def test_a_package_version_or_a_server_version_is_not_an_api_version():
    assert _check("versions_that_are_not_api.py") == []


def test_a_generated_module_may_list_its_versions():
    assert _check("generated_module.py") == []


def _check_sessions(allow=None):
    findings = check_callers(CALLERS / "sessions", SCHEMA, generated={"demo"}, allow=allow or Allow(), sessions=True)
    return [f.message for f in findings]


def test_a_method_the_session_class_lacks_is_reported_and_inherited_ones_are_not():
    assert _check_sessions() == [
        "self._session.finalize_config is not a method of the Session class",
        "session.finalize_config is not a method of the Session class",
    ]


def test_session_calls_are_not_judged_unless_asked():
    findings = check_callers(CALLERS / "sessions", SCHEMA, generated={"demo"}, allow=Allow())
    assert [f.message for f in findings] == []


def test_a_session_call_without_a_session_class_is_reported(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run(session):\n    session.send_request(service='demo', action='get_all')\n"
    )
    findings = check_callers(tmp_path, SCHEMA, generated={"demo"}, allow=Allow(), sessions=True)
    assert [f.message for f in findings] == [
        f"session.send_request cannot be checked: no class Session is defined under {tmp_path}"
    ]


def test_an_exemption_that_matches_no_call_is_reported(tmp_path):
    allow_file = tmp_path / "allow.txt"
    allow_file.write_text("exempt demo.unused a call that was removed\n")
    allow = read_allow(allow_file)
    (tmp_path / "caller.py").write_text("x = 1\n")
    findings = check_callers(tmp_path / "caller.py", SCHEMA, generated={"demo"}, allow=allow)
    assert [str(f) for f in findings] == [f"{allow_file}:1: exempt demo.unused no longer matches any call: remove it"]


def test_a_session_that_inherits_from_outside_the_root_is_not_judged(tmp_path):
    (tmp_path / "session.py").write_text("from elsewhere import Base\n\nclass Session(Base):\n    pass\n")
    (tmp_path / "caller.py").write_text("def run(session):\n    session.send_request()\n")
    findings = check_callers(tmp_path, SCHEMA, generated={"demo"}, allow=Allow(), sessions=True)
    assert [f.message for f in findings] == [
        "Session inherits from Base, which is not defined under the root: its methods cannot be checked"
    ]
