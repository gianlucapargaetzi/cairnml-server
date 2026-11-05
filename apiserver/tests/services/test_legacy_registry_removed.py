"""The legacy registry and dataset list code is gone: no dataset_registry.remove and no datasets.list endpoint, and the
2.6.0 share migration (with its DatasetRegistry model) is not importable."""

from importlib.util import find_spec

from apiserver.service_repo.service_repo import ServiceRepo


def test_the_registry_removal_endpoint_is_not_registered():
    ServiceRepo.load()

    assert "dataset_registry.remove" not in ServiceRepo.endpoint_names()


def test_the_dataset_list_snapshot_endpoint_is_not_registered():
    ServiceRepo.load()

    assert "datasets.list" not in ServiceRepo.endpoint_names()


def test_the_dataset_endpoints_that_remain_are_registered():
    ServiceRepo.load()

    assert {"dataset_registry.get_all", "dataset_registry.get_registries", "dataset_registry.add",
            "datasets.visible", "datasets.delete"} <= set(ServiceRepo.endpoint_names())


def test_the_2_6_0_migration_is_not_importable():
    assert find_spec("apiserver.mongo.migrations.2_6_0") is None


def test_the_dataset_registry_model_is_gone():
    assert find_spec("apiserver.database.model.dataset_registry") is None
