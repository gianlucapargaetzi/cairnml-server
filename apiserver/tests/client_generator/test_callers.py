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
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead",
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
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead",
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
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead",
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead",
        "self._session.finalize_config is not a method of the Session class",
        "session.finalize_config is not a method of the Session class",
    ]


def test_session_methods_are_not_judged_unless_asked():
    findings = check_callers(CALLERS / "sessions", SCHEMA, generated={"demo"}, allow=Allow())
    assert [f.message for f in findings] == [
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead",
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead",
    ]


def test_a_session_call_without_a_session_class_is_reported(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run(session):\n    session.send_request(service='demo', action='get_all')\n"
    )
    findings = check_callers(tmp_path, SCHEMA, generated={"demo"}, allow=Allow(), sessions=True)
    assert [f.message for f in findings] == [
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead",
        f"session.send_request cannot be checked: no class Session is defined under {tmp_path}",
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


def _check_by_name(name, allow=None):
    findings = check_callers(CALLERS / name, SCHEMA, generated={"demo"}, allow=allow or Allow(), sessions=True)
    return [f.message for f in findings]


def test_a_session_call_that_names_its_service_and_action_is_reported_with_the_request_class():
    assert _check_by_name("literal_action.py") == [
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead"
    ]


def test_the_first_two_positional_arguments_are_a_call_by_name_too():
    assert _check_by_name("literal_action_positional.py") == [
        "get names demo.get_all by strings: build demo.GetAllRequest and send it instead"
    ]


def test_a_generated_request_class_sent_by_a_session_is_clean():
    assert _check_by_name("generated_class_sent.py") == []


def test_a_call_by_name_on_any_object_is_reported():
    assert _check("by_name_on_a_client.py") == [
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead"
    ]


def test_a_generated_module_is_not_judged_for_a_call_by_name(tmp_path):
    module = tmp_path / "generated.py"
    module.write_text(
        "# Generated by apiserver.client_generator from the cairnml-server schema. Do not edit.\n"
        "class Session:\n    def send_request(self, service, action):\n        pass\n\n"
        "def run(session):\n    session.send_request(service='demo', action='get_all')\n"
    )
    findings = check_callers(module, SCHEMA, generated={"demo"}, allow=Allow(), sessions=True)
    assert [f.message for f in findings] == []


def test_a_call_by_name_that_is_exempt_is_not_reported(tmp_path):
    allow_file = tmp_path / "allow.txt"
    allow_file.write_text("exempt demo.get_all a call that is converted in a separate change\n")
    assert _check_by_name("literal_action.py", read_allow(allow_file)) == []


def test_a_positional_call_by_name_is_reported_when_the_schema_lacks_it():
    # send_request takes the service and the action first, so it is judged even when the schema lacks the service;
    # another method is judged only for a service of the schema (`options.get("routers", ...)` is not a call of the API)
    assert _check("positional_by_name.py") == [
        "demo.nope is not in the schema",
        "routers.get_routes is not in the schema",
    ]


ALIAS = CALLERS / "alias"


def test_a_subclass_named_like_its_base_is_not_its_own_base():
    # the base is the SDK's Session, which is outside the scanned folder: its methods cannot be checked, and it is said
    assert _check_by_name("alias/agent_pkg") == [
        "Session inherits from SdkSession, which is not defined under the root: its methods cannot be checked"
    ]


def test_a_subclass_named_like_its_base_inherits_the_methods_of_the_base():
    findings = check_callers(
        ALIAS / "agent_pkg", SCHEMA, generated={"demo"}, allow=Allow(), sessions=True, bases=[ALIAS / "cairnml"]
    )
    assert [f.message for f in findings] == []


def test_a_version_in_a_test_is_reported_only_when_it_picks_the_version_of_a_request(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_versions.py").write_text(
        "def test_it(server_level, session):\n"
        "    server_level('2.9')\n"
        "    assert Session.request_version('tasks', 'get_all') == '2.9'\n"
        "    session.send_request(service='demo', action='get_all', version='2.15')\n"
    )
    findings = check_callers(tmp_path, SCHEMA, generated={"demo"}, allow=Allow())
    assert [f.message for f in findings] == [
        "'2.15' is an API version written in hand-written code: take it from the generated classes",
        "send_request names demo.get_all by strings: build demo.GetAllRequest and send it instead",
    ]
