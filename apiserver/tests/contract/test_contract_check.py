"""The contract check finds the disagreements it is meant to find, and nothing else, on a small fixture."""

from pathlib import Path

import pytest

from apiserver.client_generator.schema_model import Action, Service
from apiserver.contract.check import (
    Problem,
    call_problems,
    check_workspace,
    client_problems,
    implemented_actions,
    raw_calls,
    read_allow,
    report,
    server_problems,
)


def service(name, **actions):
    return Service(
        name=name,
        description="",
        definitions={},
        actions={
            action: Action(
                service=name,
                name=action,
                version="2.1",
                versions=("2.1",),
                description="",
                request={"properties": {field: {} for field in fields}},
                response={},
            )
            for action, fields in actions.items()
        },
    )


SCHEMA = {"queues": service("queues", get_all=["name"], create=["name"]), "tasks": service("tasks", edit=["name"])}


def test_a_schema_action_without_an_endpoint_is_a_problem():
    problems = server_problems(SCHEMA, {"queues": {"get_all", "create"}, "tasks": set()})
    assert [p.name for p in problems] == ["tasks.edit"]


def test_an_endpoint_without_a_schema_action_is_a_problem():
    problems = server_problems(SCHEMA, {"queues": {"get_all", "create", "delete"}, "tasks": {"edit"}})
    assert [p.name for p in problems] == ["queues.delete"]


def test_a_generated_client_with_an_unknown_action_or_field_is_a_problem():
    clients = {
        "queues": {"get_all": {"name"}, "remove": set()},
        "tasks": {"edit": {"name", "input"}},
        "datasets": {"x": set()},
    }
    names = sorted(p.name for p in client_problems("sdk", clients, SCHEMA, "fixture"))
    assert names == ["datasets", "queues.remove", "tasks.edit.input"]


def test_a_raw_call_to_an_unknown_action_is_a_problem():
    calls = {("queues", "get_all"): {"a.py"}, ("queues", "nonexistent"): {"b.py"}}
    assert [p.name for p in call_problems("web", calls, SCHEMA)] == ["queues.nonexistent"]


def test_the_endpoints_are_read_from_the_decorators(tmp_path):
    (tmp_path / "x.py").write_text('@endpoint("queues.get_all", min_version="2.1")\ndef f(): pass\n', encoding="utf-8")
    assert implemented_actions(tmp_path) == {"queues": {"get_all"}}


def test_the_raw_calls_are_found_and_the_skipped_folders_are_not(tmp_path):
    (tmp_path / "a.py").write_text('url = "/queues.get_all"\n', encoding="utf-8")
    (tmp_path / "backend_api" / "services").mkdir(parents=True)
    (tmp_path / "backend_api" / "services" / "b.py").write_text('url = "/queues.bogus"\n', encoding="utf-8")
    found = raw_calls(tmp_path, ["queues"], (".py",))
    assert set(found) == {("queues", "get_all")}


def test_the_allow_list_needs_a_reason_and_names_the_problem(tmp_path):
    allow = tmp_path / "allow.txt"
    allow.write_text(
        "# comment\nweb raw call to an action the server lacks queues.bogus cairnml-web #9\n", encoding="utf-8"
    )
    allowed = read_allow(allow)
    assert allowed == {"web raw call to an action the server lacks queues.bogus": "cairnml-web #9"}
    allow.write_text("web raw call to an action the server lacks queues.bogus\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_allow(allow)


def test_an_allowed_problem_passes_and_an_entry_that_matches_nothing_is_stale():
    problems = [Problem("web", "raw call to an action the server lacks", "queues.bogus", "x.ts")]
    unallowed, stale = report(
        problems,
        {
            "web raw call to an action the server lacks queues.bogus": "dead",
            "sdk client action the server lacks queues.gone": "old",
        },
    )
    assert unallowed == []
    assert stale == ["sdk client action the server lacks queues.gone"]


def _workspace(root: Path, web_call: str, agent_call: str = "queues.get_all") -> Path:
    (root / "cairnml" / "cairnml").mkdir(parents=True)
    (root / "cairnml" / "cairnml_agent").mkdir(parents=True)
    (root / "cairnml" / "cairnml_agent" / "x.py").write_text(f'call("{agent_call}")\n', encoding="utf-8")
    (root / "cairnml-web" / "src" / "api-services").mkdir(parents=True)
    (root / "cairnml-web" / "src" / "api-services" / "x.ts").write_text(
        f"const u = `${{base}}/{web_call}`;\n", encoding="utf-8"
    )
    return root


def test_an_unknown_action_added_to_a_caller_fails_the_workspace_check(tmp_path):
    """A call to an action the schema does not define is a problem, unless the allow-list names it."""
    schema_root = Path(__file__).parents[2] / "schema" / "services"
    server = tmp_path / "server"
    (server / "apiserver" / "services").mkdir(parents=True)
    (server / "apiserver" / "services" / "q.py").write_text(
        '@endpoint("queues.get_all")\ndef f(): pass\n', encoding="utf-8"
    )
    workspace = _workspace(tmp_path / "ws", "queues.definitely_not_an_action")
    unallowed, stale, _ = check_workspace(workspace, server, None, schema_root=schema_root)
    assert [p.name for p in unallowed if p.area == "web"] == ["queues.definitely_not_an_action"]
    assert stale == []


def test_the_agent_is_read_from_the_cairnml_checkout_and_a_removed_endpoint_is_a_problem(tmp_path):
    """The agent lives in cairnml/cairnml_agent. A call it makes to an action the server no longer implements is a problem."""
    schema_root = Path(__file__).parents[2] / "schema" / "services"
    server = tmp_path / "server"
    (server / "apiserver" / "services").mkdir(parents=True)
    (server / "apiserver" / "services" / "q.py").write_text(
        '@endpoint("queues.get_all")\ndef f(): pass\n', encoding="utf-8"
    )
    workspace = _workspace(tmp_path / "ws", "queues.get_all", agent_call="queues.definitely_removed")
    unallowed, stale, _ = check_workspace(workspace, server, None, schema_root=schema_root)
    assert [(p.area, p.name) for p in unallowed if p.area == "agent"] == [("agent", "queues.definitely_removed")]
    assert stale == []
