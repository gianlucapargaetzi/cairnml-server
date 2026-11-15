"""The stages of cloning a task, as functions that can be tested alone. TaskBLL.clone_task runs them in order."""
from datetime import datetime
from typing import Callable, NamedTuple, Optional, Sequence

from apiserver.apimodels.tasks import TaskInputModel
from apiserver.database.model.task.task import (
    ArtifactModes,
    DEFAULT_ARTIFACT_MODE,
    ModelItem,
    Task,
    TaskModelNames,
    TaskModelTypes,
)
from apiserver.services.utils import escape_dict_field
from apiserver.utilities.dicts import nested_set

from .artifacts import artifacts_prepare_for_save


def merged_hyperparams(
    task: Task, hyperparams: Optional[dict], hyperparams_overrides: Optional[dict]
) -> Optional[dict]:
    """The hyperparams of the clone: the given ones as they are, else the source's with the overrides set per
    section and key, else None (the clone then keeps the source's)."""
    if hyperparams:
        return hyperparams
    if not hyperparams_overrides:
        return None

    updated_hyperparams = {
        sec: {k: value for k, value in sec_data.items()}
        for sec, sec_data in (task.hyperparams or {}).items()
    }
    for section, section_data in hyperparams_overrides.items():
        for key, value in section_data.items():
            nested_set(updated_hyperparams, (section, key), value)
    return updated_hyperparams


def merged_configuration(
    task: Task, configuration: Optional[dict], configuration_overrides: Optional[dict]
) -> Optional[dict]:
    """The configuration of the clone: the given one as it is, else the source's with the overrides set per key,
    else None (the clone then keeps the source's)."""
    if configuration:
        return configuration
    if not configuration_overrides:
        return None

    updated_configuration = {k: value for k, value in (task.configuration or {}).items()}
    for key, value in configuration_overrides.items():
        updated_configuration[key] = value
    return updated_configuration


def stamped_input_models(
    input_models: Optional[Sequence[TaskInputModel]], now: datetime
) -> Optional[Sequence[ModelItem]]:
    """The given input models as model items stamped with the time, or the argument itself when none are given."""
    if not input_models:
        return input_models
    return [ModelItem(model=m.model, name=m.name, updated=now) for m in input_models]


def script_with_overrides(
    task: Task, script_overrides: Optional[dict], strip_fields: Callable[[dict], None]
) -> dict:
    """The script of the clone: the source's with the overrides applied. The overrides are stripped in place."""
    script_dict = task.script.to_proper_dict() if task.script else {}
    if script_overrides:
        strip_fields(script_overrides)
        script_dict.update(script_overrides)
    return script_dict


class ExecutionResult(NamedTuple):
    execution: dict
    input_models: Optional[Sequence[ModelItem]]
    container: Optional[dict]
    # what the legacy "parameters" or "configuration" override leaves for params_prepare_for_save; None without overrides
    legacy_params: Optional[dict]


def apply_execution_overrides(
    execution_dict: dict,
    execution_overrides: Optional[dict],
    input_models: Optional[Sequence[ModelItem]],
    container: Optional[dict],
    now: datetime,
) -> ExecutionResult:
    """Apply the execution overrides to the execution of the clone. The special keys (model, docker_cmd, the legacy
    parameters) are taken out of the overrides in place; the model and the docker command only count when no input
    models or no container were given."""
    if not execution_overrides:
        return ExecutionResult(execution_dict, input_models, container, None)

    execution_model = execution_overrides.pop("model", None)
    if not input_models and execution_model:
        input_models = [
            ModelItem(
                model=execution_model,
                name=TaskModelNames[TaskModelTypes.input],
                updated=now,
            )
        ]

    docker_cmd = execution_overrides.pop("docker_cmd", None)
    if not container and docker_cmd:
        image, _, arguments = docker_cmd.partition(" ")
        container = {"image": image, "arguments": arguments}

    artifacts_prepare_for_save({"execution": execution_overrides})

    legacy_params = {}
    for legacy_param in ("parameters", "configuration"):
        legacy_value = execution_overrides.pop(legacy_param, None)
        if legacy_value is not None:
            legacy_params = legacy_value

    escape_dict_field(execution_overrides, "model_labels")

    execution_dict.update(execution_overrides)
    return ExecutionResult(execution_dict, input_models, container, legacy_params)


def drop_queue_and_output_artifacts(execution_dict: dict) -> None:
    """A clone starts unqueued and without the artifacts the source produced (in place)."""
    artifacts = execution_dict.get("artifacts")
    if artifacts:
        execution_dict["artifacts"] = {
            k: a
            for k, a in artifacts.items()
            if a.get("mode", DEFAULT_ARTIFACT_MODE) != ArtifactModes.output
        }
    execution_dict.pop("queue", None)
