from pathlib import Path

from apiserver.client_generator.schema_model import (
    load_schema,
    reachable_definitions,
    version_key,
)

FIXTURES = Path(__file__).parent / "fixtures" / "schema"
REAL_SCHEMA = Path(__file__).parents[2] / "schema" / "services"


def test_model_lists_the_same_actions_and_versions_as_the_server_reader(tmp_path):
    # The generator reads the schema with its own loader (pyhocon only, no server configuration); the server reads it
    # with SchemaReader. This keeps the two readers equal.
    from apiserver.schema import SchemaReader

    reader = SchemaReader()
    reader.cache_path = tmp_path / "cache.json"  # not the server's own cache file
    server = reader.get_schema()
    expected = {
        f"{service_name}.{action_name}": [str(e.version) for e in group.endpoints]
        for service_name, service in server.services.items()
        for action_name, group in service.endpoint_groups.items()
    }
    model = load_schema(REAL_SCHEMA)
    actual = {
        action.full_name: [str(v) for v in action.versions]
        for service in model.values()
        for action in service.actions.values()
    }
    assert actual == expected
    assert len(actual) > 200 and sum(len(v) for v in actual.values()) > 300


def test_only_the_newest_definition_of_an_action_is_modelled():
    demo = load_schema(FIXTURES)["demo"]
    get_all = demo.actions["get_all"]
    assert get_all.version == "2.30"
    assert get_all.versions == ("2.1", "2.15", "2.30")
    assert list(get_all.request["properties"]) == ["name", "page", "tags"]
    assert get_all.description == "Lists entries"
    # the definition only the oldest version used is not reachable from the newest ones
    reachable = reachable_definitions(
        [a.request for a in demo.actions.values()] + [a.response for a in demo.actions.values()], demo.definitions
    )
    assert list(reachable) == ["entry"]


def test_versions_are_compared_as_numbers():
    assert version_key("2.9") < version_key("2.15")
