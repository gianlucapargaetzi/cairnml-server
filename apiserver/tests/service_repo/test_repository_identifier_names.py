"""One name for the repository an action acts on (``ref``; ``refs`` for several), in the schema and in the request classes. The
old names stay for one release, as deprecated aliases, on the actions listed here. See docs/compatibility/repository-identifier.md."""
import pytest
from jsonmodels import fields as jsonmodels_fields

from apiserver.schema import SchemaReader
from apiserver.service_repo import ServiceRepo
from apiserver.services import repositories, user_access  # noqa: F401  (registers the endpoints)
from apiserver.utilities.partial_version import PartialVersion

CONF = SchemaReader.root

# the 15 actions that still accept the old names; removing an alias means removing the action from this tuple
DEPRECATED_ALIASES = (
    "delete", "set_shared", "check_sharing", "link", "unlink", "assign", "unassign", "check", "refresh", "get_runs",
    "get_history", "get_dataset_versions", "get_details", "git_token", "resolve",
)
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
    elif action in DEPRECATED_ALIASES:
        assert names == ({"refs", "repositories"} if plural else {"ref", "id", "repository"}), names
    else:
        assert names == {"ref"}, f"repositories.{action} names its identifier {sorted(names)}"


def test_the_alias_list_is_the_actions_whose_request_class_has_an_alias():
    with_alias = set()
    for action in actions("repositories"):
        model = ServiceRepo._get_endpoint(f"repositories.{action}", PartialVersion("2.40")).request_data_model
        if model is not None and {"repository", "repositories"} & {name for name, _, _ in model.iterate_with_name()}:
            with_alias.add(action)
    assert with_alias == set(DEPRECATED_ALIASES)


@pytest.mark.parametrize("service", ["repositories", "user_access"])
def test_request_class_fields_equal_the_schema(service):
    for action, (version, spec) in actions(service).items():
        request = spec["request"]
        model = ServiceRepo._get_endpoint(f"{service}.{action}", PartialVersion(version)).request_data_model
        assert model is not None, f"{service}.{action} has no request class"
        fields = {name: field for _, name, field in model.iterate_with_name()}
        assert set(fields) == set(request.get("properties", {})), f"{service}.{action}"
        # jsonmodels never requires a list field: a missing list is the empty list
        required = {name for name, field in fields.items() if field.required or isinstance(field, jsonmodels_fields.ListField)}
        assert required & set(request.get("required", [])) == set(request.get("required", [])), f"{service}.{action}"
        assert {name for name, field in fields.items() if field.required} <= set(request.get("required", [])), f"{service}.{action}"
