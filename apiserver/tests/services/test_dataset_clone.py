"""datasets.clone: a client gets the address and a read token of a repository dataset it may see. A public repository needs
no token; a private one is read with the dataset's credential; a hidden dataset is refused as missing."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datasets as bll
from apiserver.database.model.dataset import Dataset
from apiserver.services import datasets as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    name = f"c-clone-{uuid4().hex[:8]}"
    yield name
    Dataset.objects(company=name).delete()


def call_as(company, user):
    return SimpleNamespace(data={}, identity=SimpleNamespace(user=user, company=company, role="user"),
                           result=SimpleNamespace(data=None), auth=None)


def record(company, owner, ref="acme/cars", kind="repo", visibility="user", reference=None):
    dataset = Dataset(id=uuid4().hex, company=company, kind=kind, ref=ref, name=ref.split("/")[-1], owner=owner,
                      visibility=visibility, state="ok", reference=reference or ref.split("/")[-1],
                      created=datetime.utcnow())
    dataset.save()
    return dataset


def test_a_public_repository_is_cloned_with_no_token(company, monkeypatch):
    dataset = record(company, "u1")
    monkeypatch.setattr(svc, "public_repo", lambda repo: True)
    call = call_as(company, "u1")
    call.data = {"ref": "cars"}
    svc.clone(call, company, None)
    assert call.result.data["repository"] == "https://github.com/acme/cars.git"
    assert call.result.data["token"] is None
    assert call.result.data["dataset"]["id"] == dataset.id


def test_a_private_repository_is_cloned_with_a_token_made_with_its_credential(company, monkeypatch):
    record(company, "u1")
    monkeypatch.setattr(svc, "public_repo", lambda repo: False)
    monkeypatch.setattr(bll, "token_for_person", lambda company_, user, dataset, repo, write, perms: {
        "token": "t-read", "expires_at": None})
    call = call_as(company, "u1")
    call.data = {"ref": "cars", "commit": "abc123"}
    svc.clone(call, company, None)
    assert call.result.data["token"] == "t-read"
    assert call.result.data["commit"] == "abc123"


def test_a_dataset_the_caller_cannot_see_is_refused_as_missing(company, monkeypatch):
    record(company, "u1", visibility="user")
    monkeypatch.setattr(svc, "public_repo", lambda repo: False)
    call = call_as(company, "u2")
    call.data = {"ref": "cars"}
    with pytest.raises(errors.bad_request.ValidationError, match="no such dataset"):
        svc.clone(call, company, None)


def test_a_registry_entry_is_not_cloned(company, monkeypatch):
    record(company, "u1", ref="owner/registry/coco", kind="registry_entry", reference="registry/coco")
    monkeypatch.setattr(svc, "public_repo", lambda repo: True)
    call = call_as(company, "u1")
    call.data = {"ref": "registry/coco"}
    with pytest.raises(errors.bad_request.ValidationError, match="only a repository dataset"):
        svc.clone(call, company, None)
