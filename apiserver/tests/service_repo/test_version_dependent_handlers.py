"""The behaviour that depends on the API version, restored from pre-2.41 (#211).

Two kinds of check. The handler dispatch: each action with several handlers serves a version with the handler that
served it at pre-2.41, the handler of a version being the newest one registered at most that version. The branches:
each version-dependent branch of the compatibility helpers and of the tasks service decides by the version the client
requested (call.requested_endpoint_version), as pre-2.41 did, not by the definition the request resolved to. The
expected values of the branch tables are what the pre-2.41 helpers (tag c1b924ec) return for the requested version.
The helpers are called with a stub call, so no database is needed.
"""

import copy
from types import SimpleNamespace

import pytest

from apiserver.service_repo import ServiceRepo
from apiserver.services import tasks as tasks_service
from apiserver.services.utils import (
    conform_output_tags,
    conform_tags,
    ModelsBackwardsCompatibility,
    DockerCmdBackwardsCompatibility,
    unescape_metadata,
)
from apiserver.utilities.partial_version import PartialVersion

ServiceRepo.load()


def _handler_name(endpoint_name: str, version: str) -> str:
    endpoint = ServiceRepo._get_endpoint(endpoint_name, PartialVersion(version))
    assert endpoint is not None, f"no handler of {endpoint_name} at {version}"
    return endpoint.func.__name__


@pytest.mark.parametrize(
    "endpoint_name,version,handler",
    [
        # events.debug_images: the 1.0 handler is below the oldest retained block (2.1), the 1.8 one serves 2.1 to 2.6
        ("events.debug_images", "2.1", "get_debug_images_v1_8"),
        ("events.debug_images", "2.6", "get_debug_images_v1_8"),
        ("events.debug_images", "2.7", "get_debug_images"),
        ("events.debug_images", "2.20", "get_debug_images"),
        # events.get_task_log: one handler per definition family, the oldest from 1.5
        ("events.get_task_log", "1.5", "get_task_log_v1_5"),
        ("events.get_task_log", "1.6", "get_task_log_v1_5"),
        ("events.get_task_log", "1.7", "get_task_log_v1_7"),
        ("events.get_task_log", "2.8", "get_task_log_v1_7"),
        ("events.get_task_log", "2.9", "get_task_log"),
        ("events.get_task_log", "2.20", "get_task_log"),
        # events.get_task_plots and events.get_multi_task_plots: the 1.8 handler serves every retained version
        ("events.get_task_plots", "2.1", "get_task_plots"),
        ("events.get_task_plots", "2.20", "get_task_plots"),
        ("events.get_multi_task_plots", "2.1", "get_multi_task_plots"),
        ("events.get_multi_task_plots", "2.26", "get_multi_task_plots"),
        # server.info: the version of the answer grows with the version of the request
        ("server.info", "2.1", "info"),
        ("server.info", "2.7", "info"),
        ("server.info", "2.8", "info_2_8"),
        ("server.info", "2.11", "info_2_8"),
        ("server.info", "2.12", "info_2_12"),
        ("server.info", "2.26", "info_2_12"),
        # users.get_all_ex
        ("users.get_all_ex", "2.1", "get_all_ex"),
        ("users.get_all_ex", "2.7", "get_all_ex"),
        ("users.get_all_ex", "2.8", "get_all_ex_2_8"),
        ("users.get_all_ex", "2.26", "get_all_ex_2_8"),
    ],
)
def test_the_handler_of_a_version_is_the_one_that_served_it_at_pre_2_41(endpoint_name, version, handler):
    assert _handler_name(endpoint_name, version) == handler


def _call(version: str, endpoint_name: str = "tasks.get_all"):
    return SimpleNamespace(
        endpoint_name=endpoint_name,
        requested_endpoint_version=PartialVersion(version),
        actual_endpoint_version=PartialVersion(version),
    )


def test_tags_are_merged_into_the_tags_field_for_a_definition_before_2_3():
    doc = {"tags": ["a"], "system_tags": ["b"]}
    conform_output_tags(_call("2.2"), doc)
    assert doc == {"tags": ["a", "b"], "system_tags": ["b"]}


def test_system_tags_stay_apart_for_a_definition_from_2_3_on():
    doc = {"tags": ["a"], "system_tags": ["b"]}
    conform_output_tags(_call("2.3"), doc)
    assert doc == {"tags": ["a"], "system_tags": ["b"]}


def _resolved_call(requested: str, actual: str, endpoint_name: str = "tasks.get_all"):
    """The call as the service repo leaves it: requested is the version of the request, actual the definition it
    resolved to, which is never above the requested version (a request above the server level resolves to the newest)"""
    return SimpleNamespace(
        endpoint_name=endpoint_name,
        requested_endpoint_version=PartialVersion(requested),
        actual_endpoint_version=PartialVersion(actual),
    )


# Each table row is (requested, resolved definition, the pre-2.41 outcome for the requested version alone).


@pytest.mark.parametrize(
    "requested,actual,merged",
    [
        ("2.2", "2.1", True),
        ("2.2", "2.2", True),
        ("2.3", "2.1", False),
        ("2.3", "2.3", False),
        ("2.41", "2.1", False),
        ("2.41", "2.41", False),
    ],
)
def test_tags_are_merged_by_the_requested_version(requested, actual, merged):
    doc = {"tags": ["a"], "system_tags": ["b"]}
    conform_output_tags(_resolved_call(requested, actual), doc)
    assert doc == {"tags": ["a", "b"] if merged else ["a"], "system_tags": ["b"]}


@pytest.mark.parametrize(
    "requested,actual,split",
    [
        ("2.2", "2.1", True),
        ("2.2", "2.2", True),
        ("2.3", "2.1", False),
        ("2.3", "2.3", False),
        ("2.41", "2.1", False),
    ],
)
def test_old_tags_are_split_by_the_requested_version(requested, actual, split):
    tags, system_tags = conform_tags(_resolved_call(requested, actual), ["development", "mine"], None)
    if split:
        assert list(tags) == ["mine"]
        assert list(system_tags) == ["development"]
    else:
        assert list(tags) == ["development", "mine"]
        assert not system_tags


@pytest.mark.parametrize(
    "requested,actual,moved",
    [
        ("2.12", "2.1", True),
        ("2.12", "2.12", True),
        ("2.13", "2.1", False),
        ("2.13", "2.13", False),
        ("2.41", "2.1", False),
    ],
)
def test_model_fields_are_moved_into_models_by_the_requested_version(requested, actual, moved):
    fields = {"execution": {"model": "m1"}, "output": {"model": "m2"}}
    ModelsBackwardsCompatibility.prepare_for_save(_resolved_call(requested, actual), fields)
    if moved:
        assert fields["execution"] == {}
        assert fields["output"] == {}
        assert [(m["name"], m["model"]) for m in fields["models"]["input"]] == [("Input Model", "m1")]
        assert [(m["name"], m["model"]) for m in fields["models"]["output"]] == [("Output Model", "m2")]
    else:
        assert fields == {"execution": {"model": "m1"}, "output": {"model": "m2"}}


@pytest.mark.parametrize(
    "requested,actual,read_back",
    [
        ("2.12", "2.1", True),
        ("2.12", "2.12", True),
        ("2.13", "2.1", False),
        ("2.13", "2.13", False),
        ("2.41", "2.1", False),
    ],
)
def test_model_fields_are_read_back_by_the_requested_version(requested, actual, read_back):
    task = {"models": {"input": [{"name": "Input", "model": "m1"}], "output": [{"name": "Output", "model": "m2"}]}}
    expected = copy.deepcopy(task)
    ModelsBackwardsCompatibility.unprepare_from_saved(_resolved_call(requested, actual), task)
    if read_back:
        assert task["execution"]["model"] == "m1"
        assert task["output"]["model"] == "m2"
    else:
        assert task == expected


@pytest.mark.parametrize(
    "requested,actual,split",
    [
        ("2.12", "2.1", True),
        ("2.12", "2.12", True),
        ("2.13", "2.1", False),
        ("2.13", "2.13", False),
        ("2.41", "2.1", False),
    ],
)
def test_docker_command_is_split_by_the_requested_version(requested, actual, split):
    fields = {"execution": {"docker_cmd": "image:1 --gpus all"}}
    DockerCmdBackwardsCompatibility.prepare_for_save(_resolved_call(requested, actual), fields)
    if split:
        assert fields == {"execution": {}, "container": {"image": "image:1", "arguments": "--gpus all"}}
    else:
        assert fields == {"execution": {"docker_cmd": "image:1 --gpus all"}}


@pytest.mark.parametrize(
    "requested,actual,built",
    [
        ("2.12", "2.1", True),
        ("2.12", "2.12", True),
        ("2.13", "2.1", False),
        ("2.13", "2.13", False),
        ("2.41", "2.1", False),
    ],
)
def test_docker_command_is_built_back_by_the_requested_version(requested, actual, built):
    task = {"container": {"image": "image:1", "arguments": "--gpus all"}}
    DockerCmdBackwardsCompatibility.unprepare_from_saved(_resolved_call(requested, actual), task)
    if built:
        assert task["execution"]["docker_cmd"] == "image:1 --gpus all"
    else:
        assert "execution" not in task


@pytest.mark.parametrize(
    "requested,actual,unescaped",
    [
        ("2.16", "2.1", False),
        ("2.16", "2.16", False),
        ("2.17", "2.1", True),
        ("2.17", "2.17", True),
        ("2.41", "2.1", True),
        ("2.41", "2.41", True),
    ],
)
def test_metadata_is_unescaped_by_the_requested_version(requested, actual, unescaped):
    docs = [{"metadata": {"k%2E1": {"key": "k%2E1"}}}]
    unescape_metadata(_resolved_call(requested, actual, "models.get_by_id"), docs)
    if unescaped:
        assert docs == [{"metadata": {"k.1": {"key": "k%2E1"}}}]
    else:
        assert docs == [{"metadata": []}]


@pytest.mark.parametrize(
    "requested,actual,copied",
    [
        ("2.8", "2.1", True),
        ("2.8", "2.8", True),
        ("2.9", "2.1", False),
        ("2.41", "2.1", False),
    ],
)
def test_legacy_parameters_are_copied_back_by_the_requested_version(requested, actual, copied):
    data = {"hyperparams": {"Args": {"epochs": {"name": "epochs", "type": "legacy", "value": "3"}}}}
    tasks_service.conform_task_data(_resolved_call(requested, actual), data)
    if copied:
        assert data["execution"]["parameters"] == {"epochs": "3"}
    else:
        assert "execution" not in data
