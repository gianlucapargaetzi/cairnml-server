import importlib.util
import logging
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from apiserver.database.model.dataset import Dataset
from apiserver.database.model.dataset_registry import DatasetRegistry
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
    """A company of its own for the test. Its rows are removed before and after, so no other test sees them."""
    name = f"c-{uuid4().hex[:8]}"
    _remove_rows(name)
    yield name
    _remove_rows(name)


def _remove_rows(name):
    for model in (Share, Dataset, UserAccess, Queue, Project):
        model.objects(company=name).delete()
    _backend_db()["company"].delete_many({"_id": name})


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
    # visibility user, but allocated to a project: the project share is still migrated
    Dataset(id="d-user-projects", company=company, kind="repo", ref="o/user-projects", name="user projects",
            owner="dataset-owner", visibility="user", projects=["p-root1"]).save()
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
    ("dataset", "d-user-projects", "projects", "p-root1", "", "read", "root-owner"),
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

    load_migration().migrate_backend(_isolated_db())

    assert shares_of(company) == EXPECTED
    for share in Share.objects(company=company):
        share.validate()


def test_running_the_migration_twice_creates_no_duplicates(company):
    build_legacy_grants(company)
    migration = load_migration()

    migration.migrate_backend(_isolated_db())
    migration.migrate_backend(_isolated_db())

    assert shares_of(company) == EXPECTED


def test_the_old_fields_are_kept(company):
    build_legacy_grants(company)
    load_migration().migrate_backend(_isolated_db())

    queue = Queue.objects(company=company, id="q-granted").get()
    assert [u.user for u in queue.users] == ["alice"]
    assert Project.objects(company=company, id="p-root1").get().queues == ["q-public-one", "q-granted"]
    assert UserAccess.objects(company=company, id="t-alice").get().granted_projects == ["p-root1"]
    assert Dataset.objects(company=company, id="e-entry").get().projects == ["p-root1"]
    assert Dataset.objects(company=company, id="d-two").get().projects == ["p-root1", "p-root2"]


def _backend_db():
    from mongoengine.connection import get_db

    return get_db(Database.backend)


class _WithoutLegacyRegistries:
    """The database as the tests of grants see it: the server-wide dataset_registry collection is empty here, because
    rows that another test or an installation left there would give every company a registry share."""

    def __init__(self, db):
        self._db = db

    def __getitem__(self, name):
        if name == "dataset_registry":
            return SimpleNamespace(find=lambda *args, **kwargs: [])
        return self._db[name]


def _isolated_db():
    return _WithoutLegacyRegistries(_backend_db())


def test_a_project_without_an_owner_writes_no_share_and_warns(company, caplog):
    orphan = Project(id="p-orphan", company=company, name="orphan", basename="orphan", created=NOW,
                     queues=["q-orphan"])
    orphan.save()
    Queue(id="q-orphan", company=company, name="orphan queue", created=NOW, created_by="queue-owner").save()
    Dataset(id="d-orphan", company=company, kind="repo", ref="o/orphan", name="orphan",
            owner="dataset-owner", visibility="projects", projects=["p-orphan"]).save()

    with caplog.at_level(logging.WARNING):
        load_migration().migrate_backend(_isolated_db())

    assert shares_of(company) == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("queue q-orphan" in w and "p-orphan" in w for w in warnings)
    assert any("dataset d-orphan" in w and "p-orphan" in w for w in warnings)


def test_a_legacy_registry_is_public_in_every_company_once(company):
    """A registry of the dataset_registry collection is public to every company's users: one share per company, and
    the company with no document of its own (a queue only) gets one too. Nothing else is written from it."""
    registry_id = f"owner/legacy-{uuid4().hex[:8]}"
    other = f"c-{uuid4().hex[:8]}"
    queue_id = f"q-{uuid4().hex[:8]}"
    backend = _backend_db()
    DatasetRegistry(id=registry_id, repository=registry_id, added=NOW, added_by="admin").save()
    backend["company"].insert_one({"_id": company})
    Queue(id=queue_id, company=other, name="queue", created=NOW, created_by="queue-owner").save()
    try:
        migration = load_migration()
        migration.migrate_backend(backend)
        migration.migrate_backend(backend)

        expected = [("registry", registry_id, "public", "", "", "read", "admin")]
        assert [s for s in shares_of(company) if s[1] == registry_id] == expected
        assert [s for s in shares_of(other) if s[1] == registry_id] == expected
        assert Share.objects(resource_id=registry_id, company__in=[company, other]).count() == 2
    finally:
        # the test database is kept between runs: nothing of this test may stay for the next one
        Share.objects(resource_id=registry_id).delete()
        DatasetRegistry.objects(id=registry_id).delete()
        Queue.objects(id=queue_id).delete()
        backend["company"].delete_one({"_id": company})
