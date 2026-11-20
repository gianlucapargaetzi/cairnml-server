import itertools
from typing import Sequence

from apiserver.apierrors import errors
from apiserver.utilities.dicts import nested_set
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper


hyperparams_legacy_type = "legacy"
hyperparams_default_section = "Args"
tf_define_section = "TF_DEFINE"


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
            # read only below 2.9: model_desc has no write conversion, decided in #313 (see bll/task/compat.py)
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
