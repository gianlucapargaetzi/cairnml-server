"""The stages of cloning a task, as functions that can be tested alone. TaskBLL.clone_task runs them in order."""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, NamedTuple, Optional, Sequence, Tuple

from apiserver.apimodels.tasks import CloneRequest, TaskInputModel
from apiserver.bll.project import ProjectBLL
from apiserver.database import utils as dbutils
from apiserver.database.model import EntityVisibility
from apiserver.database.model.task.output import Output
from apiserver.database.model.task.task import (
    ArtifactModes,
    DEFAULT_ARTIFACT_MODE,
    ModelItem,
    Models,
    Task,
    TaskModelNames,
    TaskModelTypes,
    TaskSystemTags,
)
from apiserver.service_repo.auth import Identity
from apiserver.services.utils import escape_dict, escape_dict_field
from apiserver.utilities.dicts import nested_set

from .artifacts import artifacts_prepare_for_save


@dataclass
class CloneSpec:
    """What to clone and what to change on the clone. Every field but task_id is optional: an empty value keeps what
    the source task has. The hyperparams and configuration overrides are merged into the source's, the given
    hyperparams and configuration replace them."""

    task_id: str
    name: Optional[str] = None
    comment: Optional[str] = None
    parent: Optional[str] = None
    project: Optional[str] = None
    tags: Optional[Sequence[str]] = None
    system_tags: Optional[Sequence[str]] = None
    hyperparams: Optional[dict] = None
    configuration: Optional[dict] = None
    container: Optional[dict] = None
    execution_overrides: Optional[dict] = None
    script_overrides: Optional[dict] = None
    input_models: Optional[Sequence[TaskInputModel]] = None
    validate_references: bool = False
    new_project_name: Optional[str] = None
    hyperparams_overrides: Optional[dict] = None
    configuration_overrides: Optional[dict] = None

    @classmethod
    def from_request(cls, request: CloneRequest) -> "CloneSpec":
        """The spec of a tasks.clone call (the API names the new task's fields new_task_*)."""
        return cls(
            task_id=request.task,
            name=request.new_task_name,
            comment=request.new_task_comment,
            parent=request.new_task_parent,
            project=request.new_task_project,
            tags=request.new_task_tags,
            system_tags=request.new_task_system_tags,
            hyperparams=request.new_task_hyperparams,
            configuration=request.new_task_configuration,
            container=request.new_task_container,
            execution_overrides=request.execution_overrides,
            script_overrides=request.script_overrides,
            input_models=request.new_task_input_models,
            validate_references=request.validate_references,
            new_project_name=request.new_project_name,
        )


def merged_params(task: Task, spec: CloneSpec) -> dict:
    """The hyperparams and configuration the spec changes, under the keys "hyperparams" and "configuration"."""
    params_dict = {}
    updated_hyperparams = merged_hyperparams(task, spec.hyperparams, spec.hyperparams_overrides)
    if updated_hyperparams:
        params_dict["hyperparams"] = updated_hyperparams

    updated_configuration = merged_configuration(task, spec.configuration, spec.configuration_overrides)
    if updated_configuration:
        params_dict["configuration"] = updated_configuration
    return params_dict


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
        sec: {k: value for k, value in sec_data.items()} for sec, sec_data in (task.hyperparams or {}).items()
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


def script_with_overrides(task: Task, script_overrides: Optional[dict], strip_fields: Callable[[dict], None]) -> dict:
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
            k: a for k, a in artifacts.items() if a.get("mode", DEFAULT_ARTIFACT_MODE) != ArtifactModes.output
        }
    execution_dict.pop("queue", None)


def resolve_target_project(
    spec: CloneSpec, company_id: str, user_id: str, identity: Identity
) -> Tuple[Optional[str], Optional[dict]]:
    """The project given in the spec, or the one named by new_project_name (found or created), else None (the clone
    stays in the source's project). The second value describes a project found by name: {"id", "name"}."""
    if spec.project or not spec.new_project_name:
        return spec.project, None

    project = ProjectBLL.find_or_create(
        project_name=spec.new_project_name,
        user=user_id,
        company=company_id,
        identity=identity,
        description="",
    )
    return project, {"id": project, "name": spec.new_project_name}


def clean_system_tags(input_tags: Sequence[str]) -> Sequence[str]:
    """The system tags a clone takes over: not the ones that mark the source as in development or archived."""
    if not input_tags:
        return input_tags

    return [tag for tag in input_tags if tag not in [TaskSystemTags.development, EntityVisibility.archived.value]]


def ensure_int_labels(execution: dict) -> dict:
    """Model labels are stored as ints (in place)."""
    if not execution:
        return execution

    model_labels = execution.get("model_labels")
    if model_labels:
        execution["model_labels"] = {k: int(v) for k, v in model_labels.items()}

    return execution


class ClonePieces(NamedTuple):
    """What clone_task worked out for the new task before building it."""

    project: Optional[str]
    parent: str
    script: dict
    execution: dict
    input_models: Optional[Sequence[ModelItem]]
    container: Optional[dict]
    params: dict


def build_cloned_task(
    task: Task, spec: CloneSpec, pieces: ClonePieces, company_id: str, user_id: str, now: datetime
) -> Task:
    """The new task (not saved): the source's fields, with the spec's and the worked-out pieces on top."""
    return Task(
        id=dbutils.id(),
        user=user_id,
        company=company_id,
        created=now,
        last_update=now,
        last_change=now,
        last_changed_by=user_id,
        name=spec.name or task.name,
        comment=spec.comment or task.comment,
        parent=pieces.parent,
        project=pieces.project or task.project,
        tags=spec.tags or task.tags,
        system_tags=spec.system_tags or clean_system_tags(task.system_tags),
        type=task.type,
        script=pieces.script,
        output=Output(destination=task.output.destination) if task.output else None,
        models=Models(input=pieces.input_models or task.models.input),
        container=escape_dict(pieces.container) or task.container,
        execution=ensure_int_labels(pieces.execution),
        configuration=pieces.params.get("configuration") or task.configuration,
        hyperparams=pieces.params.get("hyperparams") or task.hyperparams,
    )


def tags_to_register(
    task: Task, new_task: Task, spec: CloneSpec
) -> Tuple[Optional[Sequence[str]], Optional[Sequence[str]]]:
    """The (tags, system tags) to register for the new task's project: the given ones while the clone stays in the
    source's project, else the clone's own (copied) ones."""
    if task.project == new_task.project:
        return spec.tags, spec.system_tags
    return new_task.tags, new_task.system_tags
