"""One name for the repository an action acts on (``ref``; ``refs`` for several), in the schema and in the request classes. The
old names ``id``, ``repository`` and ``repositories`` are gone: no action declares them. See
docs/compatibility/repository-identifier.md."""

import pytest
from jsonmodels import fields as jsonmodels_fields

from apiserver.schema import SchemaReader
from apiserver.service_repo import ServiceRepo
from apiserver.services import repositories, user_access  # noqa: F401  (registers the endpoints)
from apiserver.utilities.partial_version import PartialVersion

CONF = SchemaReader.root

NO_IDENTIFIER = ("get_all", "add", "requirement")  # they name no repository (add takes an address as url)
IDENTIFIER_FIELDS = {"ref", "refs", "id", "repository", "repositories"}


def actions(service):
    conf = SchemaReader.read_file(CONF / f"{service}.conf")
    return {name: sorted(versions.items())[-1] for name, versions in conf.items() if not name.startswith("_")}


def request_of(service, action):
    return actions(service)[action][1]["request"]


@pytest.mark.parametrize("action", sorted(actions("repositories")))
def test_identifier_names(action):
    names = set(request_of("repositories", action).get("properties", {})) & IDENTIFIER_FIELDS
    plural = action == "resolve"
    if action in NO_IDENTIFIER:
        assert names == set(), f"repositories.{action} names {names}"
    else:
        assert names == ({"refs"} if plural else {"ref"}), f"repositories.{action} names {sorted(names)}"


@pytest.mark.parametrize("service", ["repositories", "user_access"])
def test_request_class_fields_equal_the_schema(service):
    for action, (version, spec) in actions(service).items():
        request = spec["request"]
        model = ServiceRepo._get_endpoint(f"{service}.{action}", PartialVersion(version)).request_data_model
        assert model is not None, f"{service}.{action} has no request class"
        fields = {name: field for _, name, field in model.iterate_with_name()}
        assert set(fields) == set(request.get("properties", {})), f"{service}.{action}"
        # jsonmodels never requires a list field: a missing list is the empty list
        required = {
            name for name, field in fields.items() if field.required or isinstance(field, jsonmodels_fields.ListField)
        }
        assert required & set(request.get("required", [])) == set(request.get("required", [])), f"{service}.{action}"
        assert {name for name, field in fields.items() if field.required} <= set(request.get("required", [])), (
            f"{service}.{action}"
        )
