import itertools
from typing import Sequence

from apiserver.apierrors import errors
from apiserver.utilities.dicts import nested_delete, nested_get, nested_set
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper
from apiserver.utilities.partial_version import PartialVersion


hyperparams_legacy_type = "legacy"
hyperparams_default_section = "Args"
tf_define_section = "TF_DEFINE"
# requests below this API version may still send the legacy execution.parameters input
legacy_params_max_version = PartialVersion("2.9")


def _get_full_param_name(param: dict) -> str:
    section = param.get("section")
    if section != tf_define_section:
        return param["name"]

    return "/".join((section, param["name"]))


def _get_legacy_params(data: dict, with_sections: bool = False) -> Sequence[dict]:
    """
    Return the legacy params of the data dict, a list of the params or of the sections' params
    """
    if not data:
        return []

    if with_sections:
        return list(itertools.chain.from_iterable(_get_legacy_params(section_data) for section_data in data.values()))

    return [param for param in data.values() if param.get("type") == hyperparams_legacy_type]


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


def params_prepare_for_save(fields: dict):
    """
    Escape all the section and param names for hyper params and configuration to make it mongo safe
    """

    def ensure_non_empty(k: str, desc: str) -> str:
        if not k:
            raise errors.bad_request.ValidationError(f"Empty {desc} name is not allowed")
        return k

    params = fields.get("hyperparams")
    if params:
        escaped_params = {
            ParameterKeyEscaper.escape(ensure_non_empty(key, "section")): {
                ParameterKeyEscaper.escape(ensure_non_empty(k, "parameter")): v for k, v in value.items()
            }
            for key, value in params.items()
        }
        fields["hyperparams"] = escaped_params

    params = fields.get("configuration")
    if params:
        escaped_params = {
            ParameterKeyEscaper.escape(ensure_non_empty(key, "configuration")): value for key, value in params.items()
        }
        fields["configuration"] = escaped_params


def params_unprepare_from_saved(fields, copy_to_legacy=False):
    """
    Unescape all section and param names for hyper params and configuration
    If copy_to_legacy is set then copy hyperparams and configuration data to the legacy location for the old clients
    """
    for param_field in ("hyperparams", "configuration"):
        params = fields.get(param_field)
        if params:
            unescaped_params = {
                ParameterKeyEscaper.unescape(key): {ParameterKeyEscaper.unescape(k): v for k, v in value.items()}
                if isinstance(value, dict)
                else value
                for key, value in params.items()
            }
            fields[param_field] = unescaped_params

    if copy_to_legacy:
        for new_params_field, old_params_field, use_sections in (
            ("hyperparams", ("execution", "parameters"), True),
            ("configuration", ("execution", "model_desc"), False),
        ):
            legacy_params = _get_legacy_params(fields.get(new_params_field), with_sections=use_sections)
            if legacy_params:
                nested_set(
                    fields,
                    old_params_field,
                    {_get_full_param_name(p): p["value"] for p in legacy_params},
                )


def _process_path(path: str):
    """
    Frontend does a partial escaping on the path so the all '.' in section and key names are escaped
    Need to unescape and apply a full mongo escaping
    """
    parts = path.split(".")
    if len(parts) < 2 or len(parts) > 4:
        raise errors.bad_request.ValidationError("invalid task field", path=path)
    return ".".join(ParameterKeyEscaper.escape(ParameterKeyEscaper.unescape(p)) for p in parts)


def escape_paths(paths: Sequence[str]) -> Sequence[str]:
    for prefix in (
        "hyperparams.",
        "-hyperparams.",
        "configuration.",
        "-configuration.",
    ):
        paths = [_process_path(path) if path.startswith(prefix) else path for path in paths]
    return paths
