from typing import Sequence

from apiserver.apierrors import errors
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper


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


def params_unprepare_from_saved(fields):
    """
    Unescape all section and param names for hyper params and configuration
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
