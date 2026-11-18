"""The version an endpoint is registered at comes from the schema: the oldest retained definition of its action.

A request is served by the newest registered implementation whose version is at most the requested version, so an
action with one definition is served at that definition's version and refused below it. An action with several
handlers (pre-2.41 handlers restored by #211) has its oldest handler registered at the oldest retained definition and
the others at a later version, never below it.
"""

import pytest

from apiserver.contract.evolution import vkey
from apiserver.service_repo import API_VERSION, ServiceRepo
from apiserver.services_schema import schema
from apiserver.utilities.partial_version import PartialVersion

ServiceRepo.load()


def _retained_versions(service: str, action: str):
    return [str(e.version) for e in schema.services[service].endpoint_groups[action].endpoints]


def _all_actions():
    for service_name, service in sorted(schema.services.items()):
        for action in sorted(service.endpoint_groups):
            yield f"{service_name}.{action}", service_name, action


def _below(version: str) -> PartialVersion:
    major, minor = version.split(".")
    assert int(minor) > 0, f"no version below {version} to test with"
    return PartialVersion(f"{major}.{int(minor) - 1}")


ALL_ACTIONS = list(_all_actions())


@pytest.mark.parametrize("full_name,service,action", ALL_ACTIONS, ids=[a[0] for a in ALL_ACTIONS])
def test_endpoint_is_registered_at_the_oldest_retained_definition(full_name, service, action):
    oldest = _retained_versions(service, action)[0]
    endpoints = ServiceRepo._endpoints.get(full_name, [])
    assert endpoints, f"{full_name} has no handler"
    # the list is sorted newest first, so the last handler is the one that serves the oldest retained definition
    assert str(endpoints[-1].min_version) == oldest
    assert all(ep.min_version >= PartialVersion(oldest) for ep in endpoints)


SINGLE_DEFINITION_ACTIONS = [a for a in ALL_ACTIONS if len(_retained_versions(a[1], a[2])) == 1]


@pytest.mark.parametrize(
    "full_name,service,action", SINGLE_DEFINITION_ACTIONS, ids=[a[0] for a in SINGLE_DEFINITION_ACTIONS]
)
def test_single_definition_is_served_at_its_version_and_refused_below(full_name, service, action):
    version = _retained_versions(service, action)[0]
    if int(version.split(".")[1]) == 0:
        pytest.skip(f"{version} has no lower minor version to check the refusal with")
    assert ServiceRepo._get_endpoint(full_name, PartialVersion(version)) is not None
    assert ServiceRepo._get_endpoint(full_name, _below(version)) is None


def test_api_version_is_the_highest_definition_in_the_schema():
    highest = max(
        (version for _, service, action in ALL_ACTIONS for version in _retained_versions(service, action)),
        key=vkey,
    )
    assert API_VERSION == highest
