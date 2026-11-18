"""The behaviour that depends on the API version, restored from pre-2.41 (#211).

Two kinds of check. The handler dispatch: each action with several handlers serves a version with the handler that
served it at pre-2.41, the handler of a version being the newest one registered at most that version. The branches:
each version-dependent branch of the compatibility helpers and of the tasks service decides by the definition the
request resolved to (call.actual_endpoint_version), so a branch is checked at a version on each side of its threshold.
The helpers are called with a stub call, so no database is needed.
"""

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


def test_a_request_is_judged_by_the_definition_it_resolved_to_not_by_its_own_version():
    call = SimpleNamespace(
        endpoint_name="tasks.get_all",
        requested_endpoint_version=PartialVersion("2.5"),
        actual_endpoint_version=PartialVersion("2.1"),
    )
    doc = {"tags": ["a"], "system_tags": ["b"]}
    conform_output_tags(call, doc)
    assert doc["tags"] == ["a", "b"]


def test_old_tags_are_split_into_user_and_system_tags_before_2_3():
    tags, system_tags = conform_tags(_call("2.2"), ["development", "mine"], None)
    assert "development" in system_tags
    assert list(tags) == ["mine"]


def test_old_tags_are_not_split_from_2_3_on():
    tags, system_tags = conform_tags(_call("2.3"), ["development", "mine"], None)
    assert list(tags) == ["development", "mine"]
    assert not system_tags


def test_the_model_fields_are_moved_into_models_before_2_13():
    fields = {"execution": {"model": "m1"}, "output": {"model": "m2"}}
    ModelsBackwardsCompatibility.prepare_for_save(_call("2.12"), fields)
    assert "model" not in fields.get("execution", {})
    assert [m["model"] for m in fields["models"]["input"]] == ["m1"]
    assert [m["model"] for m in fields["models"]["output"]] == ["m2"]


def test_the_model_fields_are_kept_from_2_13_on():
    fields = {"execution": {"model": "m1"}}
    ModelsBackwardsCompatibility.prepare_for_save(_call("2.13"), fields)
    assert fields == {"execution": {"model": "m1"}}


def test_the_model_fields_are_read_back_for_a_definition_before_2_13():
    task = {"models": {"input": [{"name": "Input", "model": "m1"}], "output": []}}
    ModelsBackwardsCompatibility.unprepare_from_saved(_call("2.12"), task)
    assert task["execution"]["model"] == "m1"


def test_the_docker_command_is_split_into_image_and_arguments_before_2_13():
    fields = {"execution": {"docker_cmd": "image:1 --gpus all"}}
    DockerCmdBackwardsCompatibility.prepare_for_save(_call("2.12"), fields)
    assert fields["container"] == {"image": "image:1", "arguments": "--gpus all"}
    assert "docker_cmd" not in fields["execution"]


def test_the_docker_command_is_kept_from_2_13_on():
    fields = {"execution": {"docker_cmd": "image:1 --gpus all"}}
    DockerCmdBackwardsCompatibility.prepare_for_save(_call("2.13"), fields)
    assert fields == {"execution": {"docker_cmd": "image:1 --gpus all"}}


def test_the_docker_command_is_built_back_before_2_13():
    task = {"container": {"image": "image:1", "arguments": "--gpus all"}}
    DockerCmdBackwardsCompatibility.unprepare_from_saved(_call("2.12"), task)
    assert task["execution"]["docker_cmd"] == "image:1 --gpus all"


def test_metadata_is_not_returned_to_a_definition_up_to_2_16():
    docs = [{"metadata": {"k.1": {"key": "k.1"}}}]
    unescape_metadata(_call("2.16", "models.get_all"), docs)
    assert docs == [{"metadata": []}]


def test_metadata_keys_are_unescaped_from_a_definition_after_2_16():
    docs = [{"metadata": {"k%2E1": {"key": "k%2E1"}}}]
    unescape_metadata(_call("2.17", "models.get_all"), docs)
    assert docs[0]["metadata"] == {"k.1": {"key": "k%2E1"}}


def test_the_legacy_parameters_are_copied_back_for_a_definition_before_2_9():
    data = {"hyperparams": {"Args": {"epochs": {"name": "epochs", "type": "legacy", "value": "3"}}}}
    tasks_service.conform_task_data(_call("2.8"), data)
    assert data["execution"]["parameters"] == {"epochs": "3"}


def test_the_legacy_parameters_are_not_copied_back_from_2_9_on():
    data = {"hyperparams": {"Args": {"epochs": {"name": "epochs", "type": "legacy", "value": "3"}}}}
    tasks_service.conform_task_data(_call("2.9"), data)
    assert "execution" not in data
