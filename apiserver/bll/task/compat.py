"""Task field rules that depend on the API version a request was made at.

Every rule here takes the requested endpoint version (call.requested_endpoint_version, PR #236). The 2.41 removals
compare the actual version and are not part of this module. Each threshold is defined once, here.
"""

from apiserver.apierrors import errors
from apiserver.bll.task.param_utils import (
    hyperparams_default_section,
    hyperparams_legacy_type,
    params_unprepare_from_saved,
    tf_define_section,
)
from apiserver.utilities.dicts import nested_delete, nested_get, nested_set
from apiserver.utilities.partial_version import PartialVersion

# requests below this version may still send the legacy execution.parameters input, and get it copied back on read
LEGACY_PARAMS_BELOW = PartialVersion("2.9")


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
    request below LEGACY_PARAMS_BELOW
    """
    return requested_version < LEGACY_PARAMS_BELOW
