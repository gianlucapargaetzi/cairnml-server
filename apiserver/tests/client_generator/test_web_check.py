from pathlib import Path

from apiserver.client_generator.callers import Allow
from apiserver.client_generator.schema_model import load_schema
from apiserver.client_generator.web_check import check_web, interface_fields
from apiserver.client_generator.web_renderer import UNVERSIONED_ACTIONS

SCHEMA = load_schema(Path(__file__).parents[2] / "schema" / "services")


def call_path(call):
    """The path a generated service posts to: the version of the action's definition, then service.action (an action
    of UNVERSIONED_ACTIONS without a version)."""
    service, action = call.split(".")
    actions = SCHEMA[service].actions if service in SCHEMA else {}
    if call in UNVERSIONED_ACTIONS:
        return call
    version = actions[action].version if action in actions else "2.1"
    return f"v{version}/{call}"


def web(tmp_path, call="queues.get_by_id", fields=("queue",)):
    (tmp_path / "api-services").mkdir()
    (tmp_path / "model" / "queues").mkdir(parents=True)
    (tmp_path / "api-services" / "queues.service.ts").write_text(
        "this.apiRequest.post<X>(`${this.basePath}/" + call_path(call) + "`,\n"
    )
    body = "".join(f"    /** doc {{ braces }} */\n    {f}?: string;\n" for f in fields)
    (tmp_path / "model" / "queues" / "queuesGetByIdRequest.ts").write_text(
        "export interface QueuesGetByIdRequest {\n"
        + body
        + "}\n\nexport namespace QueuesGetByIdRequest {\n    export const A = 1;\n}\n"
    )
    return tmp_path


def test_a_web_layer_that_matches_the_schema_has_no_findings(tmp_path):
    assert check_web(web(tmp_path), SCHEMA, Allow()) == []


def test_a_call_to_an_action_the_schema_lacks_is_reported(tmp_path):
    findings = check_web(web(tmp_path, call="queues.no_such_action"), SCHEMA, Allow())
    assert [f.message for f in findings] == ["queues.no_such_action is not an action of the server's schema"]


def test_a_field_the_schema_lacks_is_reported(tmp_path):
    findings = check_web(web(tmp_path, fields=("queue", "bogus")), SCHEMA, Allow())
    assert (
        len(findings) == 1 and "['bogus']" in findings[0].message and "queues.get_by_id request" in findings[0].message
    )


def test_an_exemption_silences_the_drift_and_a_stale_one_is_reported(tmp_path):
    root = web(tmp_path, fields=("queue", "bogus"))
    assert check_web(root, SCHEMA, Allow({"queues.get_by_id": "tracked"})) == []
    stale = check_web(root, SCHEMA, Allow({"queues.get_by_id": "tracked", "queues.delete": "old"}))
    assert [f.message for f in stale] == ["exempt queues.delete no longer matches anything: remove it"]


def test_interface_fields_ignores_nested_objects_and_namespaces():
    text = "export interface A {\n    x?: { y: string; };\n    z: number;\n}\nexport namespace A {\n    q: 1;\n}\n"
    assert interface_fields(text, "A") == {"x", "z"}


def test_a_call_at_another_version_than_its_definition_is_reported(tmp_path):
    root = web(tmp_path)
    service = root / "api-services" / "queues.service.ts"
    version = SCHEMA["queues"].actions["get_by_id"].version
    service.write_text(service.read_text().replace(f"/v{version}/", "/v999.0/"))
    findings = check_web(root, SCHEMA, Allow())
    assert [f.message for f in findings] == [f"queues.get_by_id is sent at version 999.0, its definition is {version}"]


def test_a_call_without_a_version_is_reported(tmp_path):
    root = web(tmp_path)
    service = root / "api-services" / "queues.service.ts"
    version = SCHEMA["queues"].actions["get_by_id"].version
    service.write_text(service.read_text().replace(f"/v{version}/", "/"))
    findings = check_web(root, SCHEMA, Allow())
    assert [f.message for f in findings] == [
        f"queues.get_by_id is sent without a version, its definition is {version}: /v<version>/<service>.<action>"
    ]


def test_an_unversioned_action_is_called_without_a_version(tmp_path):
    root = web(tmp_path, call="login.logout", fields=())
    assert "}/login.logout`" in (root / "api-services" / "queues.service.ts").read_text()
    assert check_web(root, SCHEMA, Allow()) == []


def test_an_unversioned_action_called_with_a_version_is_reported(tmp_path):
    root = web(tmp_path, call="login.logout", fields=())
    service = root / "api-services" / "queues.service.ts"
    service.write_text(service.read_text().replace("}/login.logout", "}/v2.13/login.logout"))
    findings = check_web(root, SCHEMA, Allow())
    assert [f.message for f in findings] == [
        "login.logout is sent at version 2.13: it is called without a version (the path of its ID-token cookie has no version)"
    ]
