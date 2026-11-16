"""Every version the schema declares for an endpoint has an implementation the server serves at that version.

The rule, for each endpoint and each version block after the first:
- a block that is the same as the one below it is served by that implementation (nothing changed, nothing to declare);
- a block that differs from the one below it must have its own implementation, declared at that version.

A block marked removed is not served: the endpoint must answer that version with a refusal, not with an older
implementation. That refusal does not exist yet, so a removed block is reported as a known gap until it does.
"""

import pytest

from apiserver.contract.evolution import load_api, vkey
from apiserver.contract.check import SCHEMA_ROOT
from apiserver.service_repo import ServiceRepo
from apiserver.utilities.partial_version import PartialVersion

ServiceRepo.load()
API = load_api(SCHEMA_ROOT)


def _blocks_needing_an_implementation():
    for name, endpoint in sorted(API.items()):
        versions = sorted(endpoint.versions, key=vkey)
        for below, version in zip(versions, versions[1:]):
            block = endpoint.versions[version]
            if block.get("removed") is True:
                continue
            if endpoint.versions[below] != block:
                yield name, version


@pytest.mark.parametrize("name,version", list(_blocks_needing_an_implementation()))
def test_a_changed_version_has_its_own_implementation(name, version):
    endpoint = ServiceRepo._get_endpoint(name, PartialVersion(version))
    assert endpoint is not None, f"{name} has no implementation at {version}"
    assert (
        str(endpoint.min_version) == version
    ), f"{name} at {version} is served by the implementation of {endpoint.min_version}"


def test_every_block_is_served_by_some_implementation():
    missing = []
    for name, endpoint in sorted(API.items()):
        for version, block in endpoint.versions.items():
            if block.get("removed") is True:
                continue
            if ServiceRepo._get_endpoint(name, PartialVersion(version)) is None:
                missing.append(f"{name} {version}")
    assert missing == []


def test_no_implementation_is_declared_at_a_version_the_schema_does_not_have():
    orphans = []
    for name, implementations in ServiceRepo._endpoints.items():
        if name not in API:
            continue
        declared = {v for v in API[name].versions}
        for implementation in implementations:
            if str(implementation.min_version) not in declared:
                orphans.append(f"{name} {implementation.min_version}")
    assert orphans == []
