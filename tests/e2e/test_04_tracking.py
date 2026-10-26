"""Section 3 of docs/e2e_testing/E2E.md: metrics, parameters, single values, console, text and debug samples.

A run is recorded by a script on the client machine. Every check reads the run back from the server in another
process, so only what the server stored can pass.
"""

import json

import pytest

from support import workspace
from support.workspace import new_name, run_json


@pytest.fixture(scope="module")
def tracked_run(client_home, project):
    name = new_name("tracking")
    recorded = run_json(client_home, "track_example.py", [
        "--project", project, "--name", name, "--steps", "100",
    ])
    readback = run_json(client_home, "track_read.py", ["--task-id", recorded["task_id"]])
    return {"name": name, "task_id": recorded["task_id"], "read": readback}


def test_scalars_keep_every_point_of_both_series(tracked_run):
    """Every scalar point is stored.

    Expect: Both series hold 100 points.
    """
    series = tracked_run["read"]["series"]
    assert series["loss/train"]["points"] == 100
    assert series["loss/val"]["points"] == 100


def test_scalars_keep_the_iteration_of_each_point(tracked_run):
    """Each point keeps its iteration.

    Expect: Both series start at iteration 0 and end at iteration 99.
    """
    series = tracked_run["read"]["series"]
    for name in ("loss/train", "loss/val"):
        assert series[name]["first_iteration"] == 0
        assert series[name]["last_iteration"] == 99


def test_scalar_values_are_the_ones_reported(tracked_run):
    """The stored scalar values are the ones reported.

    Expect: The first and last values equal the reported ones: loss/train 1.0 to 0.01, loss/val 2.0 to 0.02.
    """
    series = tracked_run["read"]["series"]
    assert series["loss/train"]["first_value"] == pytest.approx(1.0)
    assert series["loss/train"]["last_value"] == pytest.approx(1.0 / 100)
    assert series["loss/val"]["first_value"] == pytest.approx(2.0)
    assert series["loss/val"]["last_value"] == pytest.approx(2.0 / 100)


def test_parameters_are_stored_in_the_configuration(tracked_run):
    """Parameters are stored in the run's configuration.

    Expect: General/lr is 0.01 and General/epochs is 3.
    """
    params = tracked_run["read"]["params"]
    assert params["General/lr"] == "0.01"
    assert params["General/epochs"] == "3"


def test_set_parameters_replaces_the_whole_set_and_set_parameter_adds(client_home, project):
    """Documented SDK behaviour: a group write replaces the parameters, a single write adds to them.

    Expect: After a group write only the group's parameters remain, and a single write adds to them.
    """
    recorded = run_json(client_home, "param_probe.py", ["--project", project, "--name", new_name("params")])
    assert recorded["kept"] == {"General/epochs": "3", "General/lr": "0.01"}
    assert recorded["after_group"] == {"General/epochs": "3"}


def test_single_value_appears_in_the_summary(tracked_run):
    """A single value shows in the run's summary.

    Expect: The accuracy single value is present in the summary.
    """
    assert tracked_run["read"]["single_values"].get("accuracy") is not None


def test_console_line_is_stored(tracked_run):
    """The console output is stored.

    Expect: The line the example script printed is in the stored log.
    """
    assert tracked_run["read"]["console_has_example_line"] is True


def test_debug_image_is_stored(tracked_run):
    """A debug image is stored.

    Expect: At least one debug sample is stored for the run.
    """
    assert tracked_run["read"]["debug_samples"] >= 1


def test_run_is_completed_after_it_closes(tracked_run):
    """A run is completed once it closes.

    Expect: The run's status ends with 'completed'.
    """
    assert tracked_run["read"]["status"].endswith("completed")


def test_run_is_listed_under_its_project(client_home, project, tracked_run):
    """A run is listed under its project.

    Expect: Exactly one task with the run's name is under the session project, and it is this run.
    """
    api = workspace.device_api(client_home)
    project_id = api.authed("projects.get_all", {"name": f"^{project}$"})["projects"][0]["id"]
    tasks = api.authed("tasks.get_all", {"project": [project_id], "name": f"^{tracked_run['name']}$"})["tasks"]
    assert [task["id"] for task in tasks] == [tracked_run["task_id"]]


def test_two_runs_of_one_name_are_two_experiments(client_home, project, tmp_path):
    """Two runs with one name are two experiments.

    Expect: Both tasks are listed under that name, with different ids.
    """
    name = new_name("repeated")
    # both runs are recorded by one process, one after another (docs/e2e_testing/E2E.md, speed section, item 12)
    (tmp_path / "runs.json").write_text(json.dumps([{"name": name, "steps": 3}, {"name": name, "steps": 3}]))
    first, second = workspace.run_json_lines(client_home, "track_example.py",
                                             ["--project", project, "--batch", tmp_path / "runs.json"])
    assert first["task_id"] != second["task_id"]
    api = workspace.device_api(client_home)
    project_id = api.authed("projects.get_all", {"name": f"^{project}$"})["projects"][0]["id"]
    tasks = api.authed("tasks.get_all", {"project": [project_id], "name": f"^{name}$"})["tasks"]
    assert {task["id"] for task in tasks} == {first["task_id"], second["task_id"]}


def test_run_without_a_project_name_goes_to_the_default_project(client_home):
    """The SDK does not refuse an empty project name: the run goes to the default project, with an id.

    Expect: The run succeeds and its task has a project.
    """
    result = workspace.run_raw(client_home, "track_example.py", ["--project", "", "--name", "no-project", "--steps", "1"])
    assert result.returncode == 0, result.output
    import json

    task_id = next(json.loads(line)["task_id"] for line in result.stdout.splitlines() if line.startswith("{"))
    api = workspace.device_api(client_home)
    task = api.authed("tasks.get_by_id", {"task": task_id})["task"]
    try:
        assert task["project"], "the run must be filed under a project"
    finally:
        api.authed("tasks.delete", {"task": task_id, "force": True})  # the default project is not the session's
