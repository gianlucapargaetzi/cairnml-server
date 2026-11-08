"""The company cloud storage settings (AWS, Google, Azure) are gone: no storage.* endpoint, no settings model.

The internal object store (datachunks, dataset credentials) is not covered by these checks.
"""

from importlib.util import find_spec

from apiserver.service_repo.service_repo import ServiceRepo


def test_no_storage_endpoint_is_registered():
    ServiceRepo.load()

    storage_endpoints = [name for name in ServiceRepo.endpoint_names() if name.startswith("storage.")]

    assert storage_endpoints == []


def test_the_storage_settings_model_is_gone():
    assert find_spec("apiserver.database.model.storage_settings") is None


def test_the_storage_bll_is_gone():
    assert find_spec("apiserver.bll.storage") is None
