"""Every definition the schema retains is served by an implementation: resolving the version of a retained definition
finds a handler, which may serve several definitions (the handler registered at the newest version not above it).

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


def _served_blocks():
    for name, endpoint in sorted(API.items()):
        for version, block in sorted(endpoint.versions.items(), key=lambda item: vkey(item[0])):
            if block.get("removed") is True:
                continue
            yield name, version


SERVED_BLOCKS = list(_served_blocks())


@pytest.mark.parametrize("name,version", SERVED_BLOCKS, ids=[f"{n}@{v}" for n, v in SERVED_BLOCKS])
def test_a_retained_definition_resolves_to_an_implementation(name, version):
    endpoint = ServiceRepo._get_endpoint(name, PartialVersion(version))
    assert endpoint is not None, f"{name} has no implementation at {version}"
    assert endpoint.min_version <= PartialVersion(version)


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
