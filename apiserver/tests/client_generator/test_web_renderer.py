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
    assert sorted(p.name for p in static.glob("*.ts")) == ["configuration.ts", "encoder.ts", "variables.ts"]
