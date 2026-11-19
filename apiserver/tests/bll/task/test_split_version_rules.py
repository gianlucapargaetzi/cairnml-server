"""The 2.13 model and docker command rules of bll/task/compat.py, each called below, at and above the threshold.

The version passed is the requested endpoint version (call.requested_endpoint_version). A stub call is enough: no
database is needed.
"""

from types import SimpleNamespace

import pytest

from apiserver.bll.task.compat import DockerCmdBackwardsCompatibility, ModelsBackwardsCompatibility
from apiserver.utilities.partial_version import PartialVersion


def _call(version: str):
    return SimpleNamespace(
        endpoint_name="tasks.get_all",
        requested_endpoint_version=PartialVersion(version),
        actual_endpoint_version=PartialVersion(version),
    )


@pytest.mark.parametrize("requested,split", [("2.12", True), ("2.13", False), ("2.14", False)])
def test_model_fields_are_moved_into_models_only_below_2_13(requested, split):
    fields = {"execution": {"model": "m1"}, "output": {"model": "m2"}}

    ModelsBackwardsCompatibility.prepare_for_save(_call(requested), fields)

    if split:
        assert fields["execution"] == {}
        assert fields["output"] == {}
        assert [m["model"] for m in fields["models"]["input"]] == ["m1"]
        assert [m["model"] for m in fields["models"]["output"]] == ["m2"]
    else:
        assert fields == {"execution": {"model": "m1"}, "output": {"model": "m2"}}


@pytest.mark.parametrize("requested,read_back", [("2.12", True), ("2.13", False), ("2.14", False)])
def test_model_fields_are_read_back_only_below_2_13(requested, read_back):
    task = {"models": {"input": [{"name": "Input", "model": "m1"}], "output": [{"name": "Output", "model": "m2"}]}}

    ModelsBackwardsCompatibility.unprepare_from_saved(_call(requested), task)

    if read_back:
        assert task["execution"]["model"] == "m1"
        assert task["output"]["model"] == "m2"
    else:
        assert "execution" not in task
        assert "output" not in task


@pytest.mark.parametrize("requested,split", [("2.12", True), ("2.13", False), ("2.14", False)])
def test_docker_command_is_split_only_below_2_13(requested, split):
    fields = {"execution": {"docker_cmd": "image:1 --gpus all"}}

    DockerCmdBackwardsCompatibility.prepare_for_save(_call(requested), fields)

    if split:
        assert fields == {"execution": {}, "container": {"image": "image:1", "arguments": "--gpus all"}}
    else:
        assert fields == {"execution": {"docker_cmd": "image:1 --gpus all"}}


@pytest.mark.parametrize("requested,built", [("2.12", True), ("2.13", False), ("2.14", False)])
def test_docker_command_is_built_back_only_below_2_13(requested, built):
    task = {"container": {"image": "image:1", "arguments": "--gpus all"}}

    DockerCmdBackwardsCompatibility.unprepare_from_saved(_call(requested), task)

    if built:
        assert task["execution"]["docker_cmd"] == "image:1 --gpus all"
    else:
        assert "execution" not in task
