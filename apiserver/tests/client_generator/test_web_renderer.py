from pathlib import Path

import pytest

from apiserver.client_generator.schema_model import load_schema
from apiserver.client_generator.web_renderer import render_api, render_models

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
    assert "`${this.basePath}/tasks.get_all_ex`" in api


def test_the_shared_client_files_are_in_the_generator():
    static = Path(__file__).parents[2] / "client_generator" / "web_static"
    assert sorted(p.name for p in static.glob("*.ts")) == ["api-request.ts", "api-requests.service.ts", "api.ts", "configuration.ts", "encoder.ts", "variables.ts"]


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
