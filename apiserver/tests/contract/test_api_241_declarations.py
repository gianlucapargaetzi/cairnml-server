"""What the definitions of the task endpoints and of projects.get_queues declare, per API version.

The API 2.41 cycle changed two declarations that the API change check reports against the previous release (pre-2.41):

- A task's project, parent and models are declared as the ID or the expanded object, in definitions shared by every
  version of every endpoint that uses them. The server returned the object from the endpoints that join the referenced
  documents before, and the ID from the others (test_task_references_by_version.py); the declaration was wrong. The
  requests of tasks.create, tasks.edit, tasks.validate and tasks.clone declare the same shared definition but take the
  model ID only: an object is refused.
- projects.get_queues returned the level and share_id of a queue from 2.38 on, and the schema declared them only in the
  2.41 cycle. The API change check wants a definition labelled 2.41 for it; the 2.38 definition stays as it is, so the
  contract is the same at every version.
"""

import pytest

from apiserver.apimodels.tasks import TaskInputModel
from apiserver.database.model.task.task import ModelItem
from apiserver.services_schema import schema
from apiserver.utilities.partial_version import PartialVersion


def _resolve(node, definitions, depth=0):
    while isinstance(node, dict) and "$ref" in node and depth < 12:
        node = definitions[node["$ref"].split("/")[-1]]
        depth += 1
    return node


def _walk(node, definitions, path):
    for step in path:
        node = _resolve(node, definitions)
        node = node["items"] if step == "[]" else node["properties"][step]
    return _resolve(node, definitions)


def _definition(action, version):
    service, name = action.split(".")
    return schema.services[service].endpoint_groups[name].get_for_version(PartialVersion(version))


def _request(action, version, *path):
    definition = _definition(action, version)
    return _walk(definition.request_schema, definition.definitions, path)


def _response(action, version, *path):
    definition = _definition(action, version)
    return _walk(definition.response_schema, definition.definitions, path)


def _is_id_or_object(node):
    members = node.get("anyOf") or []
    return sorted(m.get("type") for m in members) == ["object", "string"]


REQUEST_MODELS = [
    ("tasks.create", "models", "input"),
    ("tasks.create", "models", "output"),
    ("tasks.edit", "models", "input"),
    ("tasks.edit", "models", "output"),
    ("tasks.validate", "models", "input"),
    ("tasks.validate", "models", "output"),
    ("tasks.clone", "new_task_input_models"),
]


def _retained_versions(action):
    service, name = action.split(".")
    return [str(e.version) for e in schema.services[service].endpoint_groups[name].endpoints]


@pytest.mark.parametrize("path", REQUEST_MODELS, ids=lambda p: ".".join(p))
def test_a_request_declares_the_model_id_at_every_retained_version(path):
    action, *fields = path
    checked = []
    for version in _retained_versions(action):
        try:
            model = _request(action, version, *fields, "[]", "model")
        except KeyError:
            continue  # the field is not declared yet at this version
        assert "string" in [m.get("type") for m in model.get("anyOf", [model])], f"{action} at {version}: {model}"
        checked.append(version)
    assert "2.41" in checked, f"{action}: the field is not declared at 2.41"


def test_the_requests_take_the_model_id_only():
    TaskInputModel(name="in", model="m1")
    ModelItem(name="in", model="m1").validate()
    with pytest.raises(Exception, match="expected type"):
        TaskInputModel(name="in", model={"id": "m1"})
    with pytest.raises(Exception, match="only accepts string values"):
        ModelItem(name="in", model={"id": "m1"}).validate()


RESPONSES = [
    ("tasks.get_by_id", ("task",)),
    ("tasks.get_by_id_ex", ("tasks", "[]")),
    ("reports.get_task_data", ("tasks", "[]")),
]


@pytest.mark.parametrize("action,path", RESPONSES, ids=[r[0] for r in RESPONSES])
def test_the_task_models_of_a_response_are_the_id_or_the_object_at_every_retained_version(action, path):
    for version in _retained_versions(action):
        for kind in ("input", "output"):
            model = _response(action, version, *path, "models", kind, "[]", "model")
            assert _is_id_or_object(model), f"{action} at {version} {kind}: {model}"


@pytest.mark.parametrize("action,path", RESPONSES, ids=[r[0] for r in RESPONSES])
def test_the_response_declares_project_and_parent_as_the_id_or_the_object(action, path):
    for version in _retained_versions(action):
        for field in ("project", "parent"):
            assert _is_id_or_object(_response(action, version, *path, field)), (action, version, field)


QUEUE = ("queues", "[]")


@pytest.mark.parametrize(
    "version,label", [("2.38", "2.38"), ("2.39", "2.38"), ("2.40", "2.38"), ("2.41", "2.41"), ("2.50", "2.41")]
)
def test_get_queues_is_served_by_the_newest_definition_at_most_the_requested_version(version, label):
    assert str(_definition("projects.get_queues", version).version) == label


@pytest.mark.parametrize("version", ["2.38", "2.40", "2.41"])
def test_get_queues_declares_what_the_server_returns_at_every_version(version):
    queue = _response("projects.get_queues", version, *QUEUE)
    assert sorted(queue["properties"]) == ["id", "level", "name", "share_id"]
    assert queue["properties"]["level"]["enum"] == ["read", "read_write", "collaborator"]


def test_get_queues_has_a_definition_of_its_own_at_2_41_within_the_cap():
    assert _retained_versions("projects.get_queues") == ["2.38", "2.41"]
