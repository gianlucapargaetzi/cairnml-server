"""A request is served by the newest definition whose version is at most the requested one (the owner's model, #217):
an endpoint with definitions 2.50 and 2.80 is served by 2.50 at 2.60 and by 2.80 from 2.80 on, and refused below 2.50.
The definitions and their checks are tested with a synthetic group; the refusal and the API level with the real schema."""

from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.schema import EndpointVersionsGroup, SchemaReader
from apiserver.schema_rules import MAX_DEFINITIONS_PER_ENDPOINT, duplicate_definitions
from apiserver.service_repo import ServiceRepo
from apiserver.service_repo.endpoint import Endpoint
from apiserver.services_schema import schema
from apiserver.utilities.partial_version import PartialVersion

DEFAULTS = {"internal": False, "allow_roles": ["*"], "authorize": True}


def _definition(marker):
    return {
        "request": {"type": "object", "properties": {marker: {"type": "string"}}},
        "response": {"type": "object"},
    }


def _group(versions, action="act"):
    conf = {version: _definition(f"field_{version.replace('.', '_')}") for version in versions}
    return EndpointVersionsGroup("svc", action, conf=conf, defaults=DEFAULTS)


def _resolved(group, version):
    return str(group.get_for_version(PartialVersion(version)).version)


@pytest.mark.parametrize(
    "version,expected",
    [("2.50", "2.50"), ("2.60", "2.50"), ("2.79", "2.50"), ("2.80", "2.80"), ("2.99", "2.80"), ("3.0", "2.80")],
)
def test_the_newest_definition_not_above_the_requested_version_serves_it(version, expected):
    assert _resolved(_group(["2.50", "2.80"]), version) == expected


def test_a_version_below_the_oldest_definition_has_no_definition():
    group = _group(["2.50", "2.80"])
    with pytest.raises(ValueError, match="below the oldest retained definition of svc.act"):
        group.get_for_version(PartialVersion("2.49"))
    assert str(group.oldest_version) == "2.50"


def test_an_endpoint_that_never_changed_is_valid_at_its_version_and_every_later_one():
    group = _group(["2.23"])
    assert [_resolved(group, v) for v in ("2.23", "2.41", "2.50")] == ["2.23", "2.23", "2.23"]


def test_the_cap_of_definitions_per_endpoint_is_five():
    assert MAX_DEFINITIONS_PER_ENDPOINT == 5
    _group([f"2.{n}" for n in range(1, 6)])
    with pytest.raises(ValueError, match="keeps 6 definitions; the cap is 5 per endpoint"):
        _group([f"2.{n}" for n in range(1, 7)])


def test_two_definitions_of_one_version_in_one_action_are_found_in_the_text():
    text = """
ping {
  "2.9" {
    request { type = object }
  }
  "2.9" {
    request { type = object }
  }
}
other {
  "2.9" { request { type = object } }
}
"""
    assert duplicate_definitions(text) == [("ping", "2.9")]


def test_a_brace_in_a_string_or_a_comment_does_not_hide_a_duplicate():
    text = """
ping {
  // "2.9" { a comment, not a block
  "2.1" { description = "{ not a brace" }
  "2.1" { description = "}" }
}
"""
    assert duplicate_definitions(text) == [("ping", "2.1")]


def test_the_schema_files_have_no_duplicate_definitions():
    from apiserver.contract.check import SCHEMA_ROOT

    assert [p.name for p in SCHEMA_ROOT.glob("*.conf") if duplicate_definitions(p.read_text())] == []


def test_a_schema_file_with_two_blocks_of_one_version_is_rejected_when_read(tmp_path):
    path = tmp_path / "svc.conf"
    path.write_text(
        'act {\n  "2.1" { request { type = object } response { type = object } }\n'
        '  "2.1" { request { type = object } response { type = object } }\n}\n'
    )
    with pytest.raises(ValueError, match="two definitions of one version in one action: act 2.1"):
        SchemaReader.read_file(path)


def _real_action_with_an_older_version():
    for service_name, service in sorted(schema.services.items()):
        for action, group in sorted(service.endpoint_groups.items()):
            oldest = group.oldest_version
            if int(str(oldest).split(".")[1]) > 0:
                return f"{service_name}.{action}", oldest
    raise AssertionError("no action with a minor version above 0")


def _stub_call(name, version):
    """The parts of an APICall that the resolution reads and sets."""
    call = SimpleNamespace(
        endpoint_name=name,
        requested_endpoint_version=PartialVersion(version),
        actual_endpoint_version=None,
        log_api=True,
        failed=False,
        result=None,
    )

    def set_error_result(msg, code, subcode):
        call.failed = True
        call.error = (msg, code, subcode)

    call.set_error_result = set_error_result
    return call


def test_a_request_below_the_oldest_definition_is_refused_with_the_endpoint_version_and_the_oldest_supported_one():
    name, oldest = _real_action_with_an_older_version()
    below = f"{str(oldest).split('.')[0]}.{int(str(oldest).split('.')[1]) - 1}"
    call = _stub_call(name, below)

    assert ServiceRepo._resolve_endpoint_from_call(call) is None

    message, code, subcode = call.error
    assert message == (
        f"Endpoint {name} is not available at API version {below}; the oldest supported version is {oldest}"
    )
    assert (code, subcode) == (404, 1)


def test_the_refusal_is_the_unsupported_endpoint_version_error_of_the_404_group():
    error = errors.not_found.UnsupportedEndpointVersion(replacement_msg="m")
    assert (error.code, error.subcode, str(error)) == (404, 1, "m")


def test_a_request_for_an_unknown_endpoint_keeps_the_unable_to_find_error():
    call = _stub_call("nosuch.action", "2.41")
    assert ServiceRepo._resolve_endpoint_from_call(call) is None
    assert call.error[0].startswith("Unable to find endpoint for name nosuch.action")
    assert call.error[1:] == (404, 0)


@pytest.mark.parametrize("version", ["LEVEL", "999.0"])
def test_a_request_at_or_above_the_api_level_is_served_by_the_newest_definition(version):
    ServiceRepo.load()
    name = "debug.ping"
    newest = schema.services["debug"].endpoint_groups["ping"].endpoints[-1].version
    call = _stub_call(name, str(ServiceRepo.max_endpoint_version()) if version == "LEVEL" else version)

    assert ServiceRepo._resolve_endpoint_from_call(call) is not None
    assert call.actual_endpoint_version == newest


def test_an_unversioned_path_means_the_newest_definition():
    ServiceRepo.load()
    version, name = ServiceRepo.parse_endpoint_path("/debug.ping")
    assert name == "debug.ping" and version == ServiceRepo.max_endpoint_version()


def test_the_api_level_is_the_highest_definition_label_of_the_schema():
    highest = max(
        group.endpoints[-1].version
        for service in schema.services.values()
        for group in service.endpoint_groups.values()
    )
    assert ServiceRepo.max_endpoint_version() == highest == schema.newest_version()


def test_schema_validation_uses_the_definition_that_serves_the_request(monkeypatch):
    two = EndpointVersionsGroup(
        "debug",
        "ping",
        conf={
            "2.50": {
                "request": {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": True},
                "response": {"type": "object"},
            },
            "2.80": {
                "request": {
                    "type": "object",
                    "properties": {"b": {"type": "integer"}},
                    "required": ["b"],
                    "additionalProperties": True,
                },
                "response": {"type": "object"},
            },
        },
        defaults=DEFAULTS,
    )
    monkeypatch.setitem(schema.services["debug"].endpoint_groups, "ping", two)
    handler = Endpoint(name="debug.ping", func=lambda *_: None, min_version="2.50", validate_schema=True)

    old_request_validator, _ = handler.schema_validators(PartialVersion("2.60"))
    new_request_validator, _ = handler.schema_validators(PartialVersion("2.80"))

    assert old_request_validator.enabled and new_request_validator.enabled
    old_request_validator.fast_validate({"a": 1})
    with pytest.raises(Exception):
        new_request_validator.fast_validate({"a": 1})
    new_request_validator.fast_validate({"b": 1})


def test_an_action_header_written_with_a_colon_starts_its_own_scope():
    text = """
clear {
  "2.31" { request { type = object } }
}
move: {
  "2.4" { request { type = object } }
  "2.4" { request { type = object } }
}
"""
    assert duplicate_definitions(text) == [("move", "2.4")]


def test_a_reference_written_as_a_definition_is_checked_for_duplicates():
    text = """
ping {
  "2.1": ${other."2.1"}
  "2.1" { request { type = object } }
}
"""
    assert duplicate_definitions(text) == [("ping", "2.1")]


def _one_minor_below(version):
    major, minor = (int(part) for part in str(version).split("."))
    assert minor > 0, f"{version} is the oldest possible minor version of its major: no version below it"
    return f"{major}.{minor - 1}"


def _every_retained_definition():
    return [
        (f"{service_name}.{action}", str(endpoint.version))
        for service_name, service in sorted(schema.services.items())
        for action, group in sorted(service.endpoint_groups.items())
        for endpoint in group.endpoints
    ]


def _every_action_oldest_definition():
    return sorted({(name, version) for name, version in _every_retained_definition() if _is_oldest(name, version)})


def _is_oldest(name, version):
    service_name, action = name.split(".", 1)
    return str(schema.services[service_name].endpoint_groups[action].oldest_version) == version


@pytest.mark.parametrize("name,version", _every_retained_definition(), ids=lambda value: str(value))
def test_every_retained_definition_is_found_at_its_own_version(name, version):
    ServiceRepo.load()
    call = _stub_call(name, version)

    assert ServiceRepo._resolve_endpoint_from_call(call) is not None
    assert str(call.actual_endpoint_version) == version


@pytest.mark.parametrize("name,oldest", _every_action_oldest_definition(), ids=lambda value: str(value))
def test_one_version_below_the_oldest_definition_of_every_action_is_refused(name, oldest):
    ServiceRepo.load()
    below = _one_minor_below(oldest)
    call = _stub_call(name, below)

    assert ServiceRepo._resolve_endpoint_from_call(call) is None
    assert call.error[0] == (
        f"Endpoint {name} is not available at API version {below}; the oldest supported version is {oldest}"
    )
    assert call.error[1:] == (404, 1)
