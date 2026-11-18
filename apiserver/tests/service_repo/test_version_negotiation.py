"""The server serves exactly one API version. The negotiation stays in place for versions added later: a request gets the
implementation with the highest min_version that is not above the requested version, and nothing below the lowest one.
The tests use a private registry, so no real endpoint is involved."""

import pytest

from apiserver.service_repo import API_VERSION, endpoint
from apiserver.service_repo.endpoint import Endpoint
from apiserver.service_repo.service_repo import ServiceRepo
from apiserver.services_schema import schema
from apiserver.utilities.partial_version import PartialVersion


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(ServiceRepo, "_endpoints", {})
    return ServiceRepo


def _endpoint(min_version, marker):
    return Endpoint(name="debug.ping", func=lambda *_: marker, min_version=min_version)


def test_the_decorator_default_is_the_oldest_schema_definition(registry):
    @endpoint("debug.ping")
    def ping(call, company, request):
        return None

    oldest = schema.services["debug"].endpoint_groups["ping"].endpoints[0].version
    assert registry._endpoints["debug.ping"][0].min_version == oldest


def test_the_single_version_serves_its_own_version_and_nothing_older(registry):
    registry.register(_endpoint(API_VERSION, "current"))

    assert registry._get_endpoint("debug.ping", PartialVersion(API_VERSION)).func(None) == "current"
    assert registry._get_endpoint("debug.ping", PartialVersion("2.40")) is None
    assert registry._get_endpoint("debug.ping", PartialVersion("1.0")) is None


def test_a_future_version_takes_over_from_its_own_version_on(registry):
    registry.register(_endpoint(API_VERSION, "current"))
    registry.register(_endpoint("2.42", "next"))

    def served(version):
        return registry._get_endpoint("debug.ping", PartialVersion(version)).func(None)

    assert served(API_VERSION) == "current"
    assert served("2.42") == "next"
    assert served("2.50") == "next"


def test_one_implementation_per_version(registry):
    registry.register(_endpoint(API_VERSION, "current"))
    with pytest.raises(Exception, match="existing endpoint"):
        registry.register(_endpoint(API_VERSION, "again"))
