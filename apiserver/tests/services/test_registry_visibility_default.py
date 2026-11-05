# apiserver/tests/services/test_registry_visibility_default.py
"""A new registry follows the visibility rules of a dataset (Datasets spec 4a): it is user-visible by default, and the
request may set user, projects or Public. A registry is no longer public to the company by default."""

from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.dataset_registry import RegistryRequest
from apiserver.bll import datasets as access_bll
from apiserver.database.model.auth import Role
from apiserver.service_repo.auth import Identity
from apiserver.services import dataset_registry as svc
from apiserver.tests.db import ensure_db

ensure_db()

COMPANY = "c-registry"
USER = "owner"


class FakeCall:
    def __init__(self):
        self.identity = Identity(user=USER, company=COMPANY, role=Role.user)
        self.result = SimpleNamespace(data=None, data_model=None)


@pytest.fixture
def added(monkeypatch):
    """The registry add is recorded, not made: no GitHub, no database."""
    calls = []
    monkeypatch.setattr(access_bll, "add_registry", lambda company, user, url, **kwargs: calls.append((url, kwargs)))
    monkeypatch.setattr(svc, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "client_address", lambda call: "127.0.0.1")
    return calls


def test_a_new_registry_is_visible_to_its_owner_only_by_default(added):
    svc.add(FakeCall(), COMPANY, RegistryRequest(repository="acme/datasets"))

    [(url, kwargs)] = added
    assert url == "https://github.com/acme/datasets"
    assert kwargs["visibility"] == "user"


@pytest.mark.parametrize("visibility", ["user", "projects", "Public"])
def test_the_visibility_in_the_request_is_the_registry_visibility(added, visibility):
    svc.add(FakeCall(), COMPANY, RegistryRequest(repository="acme/datasets", visibility=visibility))

    [(_url, kwargs)] = added
    assert kwargs["visibility"] == visibility


def test_an_unknown_visibility_is_refused_before_anything_is_read(monkeypatch):
    def must_not_be_read(*args, **kwargs):
        raise AssertionError("the registry is read before its visibility is checked")

    monkeypatch.setattr(access_bll, "_decide", must_not_be_read)
    monkeypatch.setattr(svc, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "client_address", lambda call: "127.0.0.1")

    with pytest.raises(errors.bad_request.ValidationError):
        svc.add(FakeCall(), COMPANY, RegistryRequest(repository="acme/datasets", visibility="everyone"))

