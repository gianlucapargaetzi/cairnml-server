import importlib.util
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.queue import Queue, QueueUser
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.database import Database
from apiserver.tests.db import ensure_db

ensure_db()

MIGRATION = Path(__file__).resolve().parents[2] / "mongo" / "migrations" / "2_6_0.py"
NOW = datetime(2026, 11, 1)


def load_migration():
    spec = importlib.util.spec_from_file_location("migration_2_6_0", str(MIGRATION))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def build_legacy_grants(company):
    """One example of each field that grants access today, and the cases that must not produce a share."""
    root1 = Project(id="p-root1", company=company, user="root-owner", name="root one", basename="root1", created=NOW,
                    queues=["q-public-one", "q-granted"],
                    members=[ProjectMember(user="alice", role=ProjectRole.read, added=NOW, added_by="root-owner")])
    root2 = Project(id="p-root2", company=company, user="root-owner", name="root two", basename="root2", created=NOW)
    sub = Project(id="p-sub", company=company, user="root-owner", name="sub", basename="sub", created=NOW,
                  parent="p-root1", queues=["q-sub-only"])
    for project in (root1, root2, sub):
        project.save()

    Queue(id="q-public-one", company=company, name="public one", created=NOW, created_by="queue-owner",
          public=True).save()
    Queue(id="q-granted", company=company, name="granted", created=NOW, created_by="queue-owner",
          users=[QueueUser(user="alice", added=NOW, added_by="queue-owner")]).save()
    Queue(id="q-sub-only", company=company, name="sub only", created=NOW, created_by="queue-owner").save()

    UserAccess(id="t-alice", company=company, user="alice", kind="token", label="token", secret="x",
               granted_projects=["p-root1"]).save()
    UserAccess(id="t-bob", company=company, user="bob", kind="token", label="token", secret="x",
               granted_projects=["p-root2"]).save()

    Dataset(id="d-projects", company=company, kind="repo", ref="o/projects", name="projects",
            owner="dataset-owner", visibility="projects", projects=["p-root1"]).save()
    Dataset(id="d-public", company=company, kind="repo", ref="o/public", name="public",
            owner="dataset-owner", visibility="Public").save()
    Dataset(id="d-two", company=company, kind="repo", ref="o/two", name="two",
            owner="dataset-owner", visibility="projects", projects=["p-root1", "p-root2"]).save()
    Dataset(id="d-private", company=company, kind="repo", ref="o/private", name="private",
            owner="dataset-owner", visibility="user").save()
    Dataset(id="r-public", company=company, kind="registry", ref="o/registry-public", name="registry public",
            owner="dataset-owner", visibility="Public").save()
    Dataset(id="r-projects", company=company, kind="registry", ref="o/registry-projects", name="registry projects",
            owner="dataset-owner", visibility="projects", projects=["p-root2"]).save()
    Dataset(id="e-entry", company=company, kind="registry_entry", ref="o/registry-projects/entry", name="entry",
            owner="dataset-owner", visibility="Public", projects=["p-root1"]).save()


EXPECTED = sorted([
    # (resource_type, resource_id, scope, project, user, level, granted_by)
    # a queue's public share is granted by its owner; a project share by the owner of the root project
    ("queue", "q-public-one", "public", "", "", "read", "queue-owner"),
    ("queue", "q-public-one", "projects", "p-root1", "", "read", "root-owner"),
    ("queue", "q-granted", "user", "", "alice", "read", "queue-owner"),
    ("queue", "q-granted", "projects", "p-root1", "", "read", "root-owner"),
    # alice is a member of p-root1, so she grants her token there; bob is not a member of p-root2, so its owner does
    ("credential", "t-alice", "projects", "p-root1", "", "read", "alice"),
    ("credential", "t-bob", "projects", "p-root2", "", "read", "root-owner"),
    # the dataset owner is not a member of any project: the project owner grants the project share
    ("dataset", "d-projects", "projects", "p-root1", "", "read", "root-owner"),
    ("dataset", "d-public", "public", "", "", "read", "dataset-owner"),
    ("dataset", "d-two", "projects", "p-root1", "", "read", "root-owner"),
    ("dataset", "d-two", "projects", "p-root2", "", "read", "root-owner"),
    ("registry", "r-public", "public", "", "", "read", "dataset-owner"),
    ("registry", "r-projects", "projects", "p-root2", "", "read", "root-owner"),
    # the registry entry e-entry has no share: it inherits from its registry
])


def shares_of(company):
    return sorted(
        (s["resource_type"], s["resource_id"], s["scope"], s["project"], s["user"], s["level"], s["granted_by"])
        for s in Share._get_collection().find({"company": company})
    )


def test_each_existing_grant_becomes_one_share(company):
    build_legacy_grants(company)
    assert shares_of(company) == []

    load_migration().migrate_backend(_backend_db())

    assert shares_of(company) == EXPECTED
    for share in Share.objects(company=company):
        share.validate()


def test_running_the_migration_twice_creates_no_duplicates(company):
    build_legacy_grants(company)
    migration = load_migration()

    migration.migrate_backend(_backend_db())
    migration.migrate_backend(_backend_db())

    assert shares_of(company) == EXPECTED


def test_the_old_fields_are_kept(company):
    build_legacy_grants(company)
    load_migration().migrate_backend(_backend_db())

    queue = Queue.objects(company=company, id="q-granted").get()
    assert [u.user for u in queue.users] == ["alice"]
    assert Project.objects(company=company, id="p-root1").get().queues == ["q-public-one", "q-granted"]
    assert UserAccess.objects(company=company, id="t-alice").get().granted_projects == ["p-root1"]
    assert Dataset.objects(company=company, id="e-entry").get().projects == ["p-root1"]
    assert Dataset.objects(company=company, id="d-two").get().projects == ["p-root1", "p-root2"]


def _backend_db():
    from mongoengine.connection import get_db

    return get_db(Database.backend)
