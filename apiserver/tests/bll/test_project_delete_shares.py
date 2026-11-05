# apiserver/tests/bll/test_project_delete_shares.py
"""Deleting a project changes the old fields that pointed at it, and removes its shares: the two agree afterwards."""
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.bll import access
from apiserver.bll.project.project_cleanup import delete_project
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project
from apiserver.database.model.queue import Queue
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")


def make_project(company, id_, owner, queues=()):
    Project(id=id_, company=company, user=owner, name=id_, basename=id_, created=datetime.utcnow(),
            queues=list(queues)).save()


def shares_projects(company, resource_type, resource_id):
    return {s.project for s in Share.objects(company=company, resource_type=resource_type, resource_id=resource_id,
                                             scope="projects")}


def test_deleting_a_project_removes_it_from_the_old_fields_and_the_shares(company):
    make_project(company, "p-del", "owner1", queues=["q1"])
    make_project(company, "p-keep", "owner1", queues=["q1"])
    Queue(id="q1", company=company, name="queue one", created=datetime.utcnow(), created_by="owner1").save()
    access.record_grant(company, "owner1", "queue", "q1", "projects", "p-del", actor="owner1")
    access.record_grant(company, "owner1", "queue", "q1", "projects", "p-keep", actor="owner1")

    Dataset(id="d1", company=company, kind="repo", ref="o/d1", name="d1", owner="owner1", visibility="projects",
            projects=["p-del", "p-keep"]).save()
    access.record_grant(company, "owner1", "dataset", "d1", "projects", "p-del", actor="owner1")
    access.record_grant(company, "owner1", "dataset", "d1", "projects", "p-keep", actor="owner1")

    UserAccess(id="acc1", company=company, user="owner1", kind="token", label="t", host="github.com", path="",
               secret=store.encrypt("ghp", store.key_material()), granted_projects=["p-del", "p-keep"]).save()
    access.record_grant(company, "owner1", "credential", "acc1", "projects", "p-del", actor="owner1")
    access.record_grant(company, "owner1", "credential", "acc1", "projects", "p-keep", actor="owner1")

    delete_project(company, "owner1", "p-del", force=True, delete_contents=False, delete_external_artifacts=False)

    assert Project.objects(company=company, id="p-del").first() is None
    assert Dataset.objects(company=company, id="d1").get().projects == ["p-keep"]
    assert UserAccess.objects(company=company, id="acc1").get().granted_projects == ["p-keep"]
    assert shares_projects(company, "dataset", "d1") == {"p-keep"}
    assert shares_projects(company, "credential", "acc1") == {"p-keep"}
    assert shares_projects(company, "queue", "q1") == {"p-keep"}
    assert Project.objects(company=company, id="p-keep").get().queues == ["q1"]
