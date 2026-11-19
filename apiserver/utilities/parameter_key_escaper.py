from typing import Sequence, Union

from boltons.dictutils import OneToOne
from mongoengine.queryset.transform import MATCH_OPERATORS

from apiserver.apierrors import errors
from apiserver.utilities.dicts import nested_get, nested_set


class ParameterKeyEscaper:
    """
    Makes the fields name ready for use with MongoDB and Mongoengine
    . and $ are replaced with their codes
    __ and leading _ are escaped
    Since % is used as an escape character the % is also escaped
    """

    _mapping = OneToOne({".": "%2E", "$": "%24", "__": "%_%_"})

    @classmethod
    def escape(cls, value: str):
        """Quote a parameter key"""
        value = value.strip()
        if not value:
            raise errors.bad_request.ValidationError("Empty key is not allowed")

        value = value.replace("%", "%%")
        for c, r in cls._mapping.items():
            value = value.replace(c, r)

        if value.startswith("_"):
            value = "%_" + value[1:]

        return value

    @classmethod
    def _unescape(cls, value: str):
        for c, r in cls._mapping.inv.items():
            value = value.replace(c, r)
        return value

    @classmethod
    def unescape(cls, value: str):
        """Unquote a quoted parameter key"""
        value = "%".join(map(cls._unescape, value.split("%%")))

        if value.startswith("%_"):
            value = "_" + value[2:]

        return value


def mongoengine_safe(field_name):
    if field_name in MATCH_OPERATORS:
        return field_name + "__"
    return field_name


def escape_dict(data: dict) -> dict:
    if not data:
        return data

    return {ParameterKeyEscaper.escape(k): v for k, v in data.items()}


def unescape_dict(data: dict) -> dict:
    if not data:
        return data

    return {ParameterKeyEscaper.unescape(k): v for k, v in data.items()}


def escape_dict_field(fields: dict, path: Union[str, Sequence[str]]):
    if isinstance(path, str):
        path = (path,)

    data = nested_get(fields, path)
    if not data or not isinstance(data, dict):
        return

    nested_set(fields, path, escape_dict(data))


def unescape_dict_field(fields: dict, path: Union[str, Sequence[str]]):
    if isinstance(path, str):
        path = (path,)

    data = nested_get(fields, path)
    if not data or not isinstance(data, dict):
        return

    nested_set(fields, path, unescape_dict(data))
