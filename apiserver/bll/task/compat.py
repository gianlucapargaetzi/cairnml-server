"""Task field rules that depend on the API version a request was made at.

Every rule here takes the requested endpoint version (call.requested_endpoint_version, PR #236). The 2.41 removals
compare the actual version and are not part of this module. Each threshold is defined once, here.
"""

from datetime import datetime
from typing import Sequence, Union

from apiserver.apierrors import errors
from apiserver.bll.task.param_utils import (
    hyperparams_default_section,
    hyperparams_legacy_type,
    params_unprepare_from_saved,
    tf_define_section,
)
from apiserver.database.model.task.task import TaskModelNames, TaskModelTypes
from apiserver.service_repo import APICall
from apiserver.utilities.dicts import nested_delete, nested_get, nested_set
from apiserver.utilities.partial_version import PartialVersion

# requests below this version may still send the legacy execution.parameters input, and get it copied back on read
LEGACY_PARAMS_BELOW = PartialVersion("2.9")

# requests below this version get the models and the docker command in the legacy task fields (split on write,
# built back on read)
MODELS_AND_DOCKER_CMD_BELOW = PartialVersion("2.13")


def _remove_legacy_params(data: dict) -> int:
    """
    Remove the legacy params from the sections of the hyperparams data (in place), dropping the sections that become
    empty, and return the number of removed params
    """
    removed = 0
    if not data:
        return removed

    for section, section_data in list(data.items()):
        for key, param in list(section_data.items()):
            if param.get("type") == hyperparams_legacy_type:
                removed += 1
                del section_data[key]
        if not section_data:
            del data[section]

    return removed


def _split_legacy_param_name(full_name: str) -> tuple[str, str]:
    """
    Return the section and the name of a legacy param: TF_DEFINE/<name> is in the TF_DEFINE section, any other name
    in the default one
    """
    section, _, name = full_name.partition("/")
    if section != tf_define_section:
        return hyperparams_default_section, full_name

    if not name:
        raise errors.bad_request.ValidationError("Parameter name cannot be empty")
    return section, name


def legacy_params_prepare_for_save(fields: dict, previous_task=None):
    """
    Convert the legacy execution.parameters input into hyperparams (default section, or TF_DEFINE by name) in place.
    Without hyperparams in the request the hyperparams of the previous task are the base, minus its legacy params.
    Must run before params_prepare_for_save, which escapes the names.
    """
    legacy_params = nested_get(fields, ("execution", "parameters"))
    if legacy_params is None:
        return

    if not fields.get("hyperparams") and previous_task and previous_task.hyperparams:
        previous_data = previous_task.to_proper_dict().get("hyperparams")
        removed = _remove_legacy_params(previous_data)
        if not legacy_params and not removed:
            # only the legacy field is to be deleted and there is nothing to delete
            return

        fields_update = {"hyperparams": previous_data}
        params_unprepare_from_saved(fields_update)
        fields.update(fields_update)

    for full_name, value in legacy_params.items():
        section, name = _split_legacy_param_name(full_name)
        new_param = {"name": name, "type": hyperparams_legacy_type, "value": str(value), "section": section}
        nested_set(fields, ("hyperparams", section, name), new_param)
    nested_delete(fields, ("execution", "parameters"))


def legacy_params_on_write(requested_version: PartialVersion, fields: dict, previous_task=None):
    """
    Convert the legacy execution.parameters input on write, only for a request below LEGACY_PARAMS_BELOW
    """
    if requested_version < LEGACY_PARAMS_BELOW:
        legacy_params_prepare_for_save(fields, previous_task=previous_task)


def legacy_params_copied_back_on_read(requested_version: PartialVersion) -> bool:
    """
    Whether the hyperparams and configuration are copied back to the legacy execution fields on read, only for a
    request below LEGACY_PARAMS_BELOW.

    The copy-back of the configuration is execution.model_desc, and it is one way only: below 2.9 a client can read
    model_desc but not write it. Unlike execution.parameters (converted on write by legacy_params_on_write), model_desc
    has no write conversion. Decided on 2026-11-20 (cairnml-server#282, decision 7; issue #313): no known client writes
    it, and the pre-2.41 SDK passes its checks without a write path. Documented in documentation/api_versions.md.
    """
    return requested_version < LEGACY_PARAMS_BELOW


class ModelsBackwardsCompatibility:
    mode_to_fields = {
        TaskModelTypes.input: ("execution", "model"),
        TaskModelTypes.output: ("output", "model"),
    }
    models_field = "models"

    @classmethod
    def prepare_for_save(cls, call: APICall, fields: dict):
        if call.requested_endpoint_version >= MODELS_AND_DOCKER_CMD_BELOW:
            return

        for mode, field in cls.mode_to_fields.items():
            value = nested_get(fields, field)
            if value is None:
                continue
            val = (
                [
                    dict(
                        name=TaskModelNames[mode],
                        model=value,
                        updated=datetime.utcnow(),
                    )
                ]
                if value
                else []
            )
            nested_set(fields, (cls.models_field, mode), value=val)

            nested_delete(fields, field)

    @classmethod
    def unprepare_from_saved(cls, call: APICall, tasks_data: Union[Sequence[dict], dict]):
        if call.requested_endpoint_version >= MODELS_AND_DOCKER_CMD_BELOW:
            return

        if isinstance(tasks_data, dict):
            tasks_data = [tasks_data]

        for task in tasks_data:
            for mode, field in cls.mode_to_fields.items():
                models = nested_get(task, (cls.models_field, mode))
                if not models:
                    continue

                model = models[0] if mode == TaskModelTypes.input else models[-1]
                if model:
                    nested_set(task, field, model.get("model"))


class DockerCmdBackwardsCompatibility:
    field = ("execution", "docker_cmd")

    @classmethod
    def prepare_for_save(cls, call: APICall, fields: dict):
        if call.requested_endpoint_version >= MODELS_AND_DOCKER_CMD_BELOW:
            return

        docker_cmd = nested_get(fields, cls.field)
        if docker_cmd is not None:
            image, _, arguments = docker_cmd.partition(" ")
            nested_set(fields, ("container", "image"), value=image)
            nested_set(fields, ("container", "arguments"), value=arguments)

        nested_delete(fields, cls.field)

    @classmethod
    def unprepare_from_saved(cls, call: APICall, tasks_data: Union[Sequence[dict], dict]):
        if call.requested_endpoint_version >= MODELS_AND_DOCKER_CMD_BELOW:
            return

        if isinstance(tasks_data, dict):
            tasks_data = [tasks_data]

        for task in tasks_data:
            container = task.get("container")
            if not container or not container.get("image"):
                continue

            docker_cmd = " ".join(filter(None, map(container.get, ("image", "arguments"))))
            if docker_cmd:
                nested_set(task, cls.field, docker_cmd)
