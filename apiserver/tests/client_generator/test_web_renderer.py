import re
from pathlib import Path

import pytest

from apiserver.client_generator.python_renderer import Unsupported
from apiserver.client_generator.schema_model import load_schema
from apiserver.client_generator.web_renderer import UNVERSIONED_ACTIONS, render_api, render_endpoints, render_models
from apiserver.config_repo import config

SCHEMA = load_schema(Path(__file__).parents[2] / "schema" / "services")


@pytest.mark.parametrize("name", sorted(SCHEMA))
def test_every_service_renders_without_placeholders(name):
    service = SCHEMA[name]
    api = render_api(service)
    assert "{PARENT}" not in api and "{" + "PARENT" not in api
    for stem, text in render_models(service).items():
        assert "{PARENT}" not in text, stem
        assert "export interface" in text or "export type" in text, stem


def test_every_definition_is_written_so_the_barrel_can_export_it():
    files = render_models(SCHEMA["events"])
    assert "metricsPlotEvent" in files


def test_a_dotted_field_name_is_quoted():
    files = render_models(SCHEMA["reports"])
    text = files["reportsGetTaskDataRequest"]
    assert "'input.view.entries.dataset'?: string[];" in text


def test_an_action_request_and_response_have_their_names():
    api = render_api(SCHEMA["tasks"])
    assert "public tasksGetAllEx(request: TasksGetAllExRequest" in api
    assert f"`${{this.basePath}}/v{SCHEMA['tasks'].actions['get_all_ex'].version}/tasks.get_all_ex`" in api


def test_a_call_is_sent_at_the_version_of_its_definition_not_a_fixed_one():
    api = render_api(SCHEMA["login"])
    version = SCHEMA["login"].actions["supported_modes"].version
    assert f"`${{this.basePath}}/v{version}/login.supported_modes`" in api
    assert "/v999.0/" not in api


def test_the_unversioned_actions_are_called_without_a_version():
    api = render_api(SCHEMA["login"])
    for action in ("logout", "idp_start", "idp_callback"):
        assert f"`${{this.basePath}}/login.{action}`" in api
    assert not re.search(r"/v[\d.]+/login\.(logout|idp_start|idp_callback)", api)


def test_the_endpoints_file_has_the_version_of_every_action_and_throws_on_an_unknown_one():
    text = render_endpoints(SCHEMA)
    for service in SCHEMA.values():
        for action in service.actions.values():
            expected = "null" if action.full_name in UNVERSIONED_ACTIONS else f"'{action.version}'"
            assert f"    ['{action.full_name}', {expected}],\n" in text
    assert "export const endpointUrl = (action: string): string => {" in text
    assert "if (!ACTION_VERSIONS.has(action)) {\n        throw new Error(" in text
    assert "/v999.0" not in text


def test_the_unversioned_actions_are_actions_of_the_schema():
    for full in UNVERSIONED_ACTIONS:
        service, action = full.split(".")
        assert action in SCHEMA[service].actions


def test_a_stale_unversioned_rule_is_not_rendered(monkeypatch):
    monkeypatch.setitem(UNVERSIONED_ACTIONS, "login.gone", "no such action")
    with pytest.raises(Unsupported, match="login.gone"):
        render_endpoints(SCHEMA)


def test_the_logout_cookie_path_is_the_unversioned_logout_path():
    """The cookie that carries the ID token is sent only on a request whose path starts with its Path: the web app must
    call login.logout at exactly that path, so the action is unversioned and the config names the same path."""
    path = config.get("apiserver.auth.custom_cookies.idp_session.settings.path")
    assert path == "/cairnml/api/login.logout"
    assert "login.logout" in UNVERSIONED_ACTIONS


def test_the_shared_client_files_are_in_the_generator():
    static = Path(__file__).parents[2] / "client_generator" / "web_static"
    assert sorted(p.name for p in static.glob("*.ts")) == [
        "api-request.ts",
        "api-requests.service.ts",
        "api.ts",
        "configuration.ts",
        "encoder.ts",
        "variables.ts",
    ]


def test_the_web_services_import_by_relative_path_not_by_the_app_alias():
    api = render_api(SCHEMA["tasks"])
    assert "from '../model/tasks/" in api and "from '../../app.constants'" in api
    assert "'~/" not in api


def test_the_web_files_carry_the_block_version_of_their_definition_not_a_fixed_one():
    demo = load_schema(Path(__file__).parent / "fixtures" / "schema")["demo"]
    api = render_api(demo)
    models = render_models(demo)
    assert "OpenAPI spec version: 2.30\n" in api  # the service's highest block version
    assert "OpenAPI spec version: 2.4\n" in models["demoGetByIdRequest"]
    assert "OpenAPI spec version: 2.30\n" in models["demoGetAllRequest"]
    assert all("2.41" not in text for text in [api, *models.values()])


def test_generating_the_web_target_writes_endpoints_and_check_notices_drift(tmp_path):
    from apiserver.client_generator.__main__ import main

    out = tmp_path / "business-logic"
    assert main(["generate", "--target", "web", "--out", str(out), "--services", "login"]) == 0
    endpoints = out / "api-services" / "endpoints.ts"
    assert endpoints.read_text() == render_endpoints(SCHEMA)
    assert main(["generate", "--target", "web", "--out", str(out), "--services", "login", "--check"]) == 0
    endpoints.write_text(endpoints.read_text().replace("'", '"', 1))
    assert main(["generate", "--target", "web", "--out", str(out), "--services", "login", "--check"]) == 1
    # what it generated is what check-web accepts
    assert main(["check-web", "--root", str(out)]) == 0


def _method(api: str, name: str) -> str:
    """The text of one generated method of a service class, from its signature to its closing brace."""
    start = api.index(f"    public {name}(")
    return api[start : api.index("\n    }\n", start) + len("\n    }\n")]


def test_a_json_call_takes_the_options_of_the_call_and_passes_them_to_the_transport():
    """reports.get_task_data is a JSON POST: its options (a tenant header, the query) go to the transport, which sends
    them, and it has no address builder because it is not a GET."""
    api = render_api(SCHEMA["reports"])
    method = _method(api, "reportsGetTaskData")
    assert "public reportsGetTaskData(request: ReportsGetTaskDataRequest, options?: ApiOptions): Observable<any> {" in method
    assert "this.apiRequest.post<ReportsGetTaskDataResponse>(`${this.basePath}/v2.41/reports.get_task_data`," in method
    assert method.endswith("            options\n        );\n    }\n")
    assert "reportsGetTaskDataUrl" not in api


def test_a_file_action_returns_a_blob_and_sends_the_options_of_the_call():
    """events.download_task_log returns the log attachment: a Blob, posted with the request body and the call's options
    (the tenant header), with the response read as a file."""
    api = render_api(SCHEMA["events"])
    version = SCHEMA["events"].actions["download_task_log"].version
    method = _method(api, "eventsDownloadTaskLog")
    assert "public eventsDownloadTaskLog(request: EventsDownloadTaskLogRequest, options?: ApiOptions): Observable<Blob> {" in method
    assert f"this.apiRequest.postBlob(`${{this.basePath}}/v{version}/events.download_task_log`," in method
    assert method.endswith("            options\n        );\n    }\n")
    assert "Observable<any>" not in method


def test_a_get_action_has_a_get_method_and_a_builder_of_its_address():
    """organization.download_for_get_all is downloaded by a link (a GET): the call is a GET of the file, with the request
    as its query, and the builder gives the address of that link."""
    api = render_api(SCHEMA["organization"])
    version = SCHEMA["organization"].actions["download_for_get_all"].version
    method = _method(api, "organizationDownloadForGetAll")
    assert "public organizationDownloadForGetAll(request: OrganizationDownloadForGetAllRequest, options?: ApiOptions): Observable<Blob> {" in method
    assert f"this.apiRequest.getBlob(`${{this.basePath}}/v{version}/organization.download_for_get_all`," in method
    assert "this.apiRequest.post" not in method
    builder = _method(api, "organizationDownloadForGetAllUrl")
    assert "public organizationDownloadForGetAllUrl(request: OrganizationDownloadForGetAllRequest): string {" in builder
    assert f"return this.apiRequest.url(`${{this.basePath}}/v{version}/organization.download_for_get_all`, request);" in builder


def test_the_file_and_get_rules_name_actions_of_the_schema(monkeypatch):
    from apiserver.client_generator.web_renderer import FILE_ACTIONS, GET_ACTIONS

    monkeypatch.setitem(FILE_ACTIONS, "events.gone", "no such action")
    with pytest.raises(Unsupported, match="events.gone"):
        render_endpoints(SCHEMA)
    monkeypatch.undo()
    monkeypatch.setitem(GET_ACTIONS, "events.gone", "no such action")
    with pytest.raises(Unsupported, match="events.gone"):
        render_endpoints(SCHEMA)


def test_the_services_with_file_and_get_calls_are_what_check_web_accepts(tmp_path):
    from apiserver.client_generator.__main__ import main

    out = tmp_path / "business-logic"
    services = "events,organization,reports"
    assert main(["generate", "--target", "web", "--out", str(out), "--services", services]) == 0
    assert main(["generate", "--target", "web", "--out", str(out), "--services", services, "--check"]) == 0
    assert main(["check-web", "--root", str(out)]) == 0
