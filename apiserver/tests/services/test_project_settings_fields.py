# apiserver/tests/services/test_project_settings_fields.py
"""The project settings reads return, for each queue, dataset and credential shared with a project, the level it holds
on the project's share, the share id, and for datasets and credentials whether the share is inherited from a run. The
values are the share record's own, never read from the old fields."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.bll import user_access
from apiserver.bll.access import record_grant
from apiserver.apimodels.project_access import GetQueuesRequest
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Queue
from apiserver.database.model.user_access import UserAccess
from apiserver.bll.datasets import view
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

OWNER, MEMBER = "owner", "member"


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.requested_endpoint_version = PartialVersion("2.40")
        self.result = SimpleNamespace(data=None, data_model=None)


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def root(company):
    for user in (OWNER, MEMBER):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    project = Project(
        id=uuid4().hex, company=company, user=OWNER, name="root", basename="root", created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
        members=[ProjectMember(user=MEMBER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER)],
    )
    project.save()
    return project.id


def dataset(company):
    record = Dataset(id=uuid4().hex, company=company, kind="repo", ref="acme/cars", name="cars", owner=OWNER,
                     created=datetime.utcnow())
    record.save()
    return record


def credential(company):
    record = UserAccess(id=uuid4().hex, company=company, user=OWNER, kind="app", label="token", host="github.com",
                        path="", secret="s3cr3t", created=datetime.utcnow())
    record.save()
    return record


def project_share(rows, root):
    return next(row for row in rows if row["project"] == root)


def test_a_manual_dataset_share_shows_its_level_share_id_and_is_not_inherited(company, root):
    record = dataset(company)
    share = record_grant(company, OWNER, "dataset", record.id, "projects", root, level="read_write", actor=OWNER)

    shown = project_share(view(record)["project_shares"], root)

    assert shown == {"project": root, "level": "read_write", "share_id": share.id, "inherited": False}


def test_a_share_created_by_a_run_shows_as_inherited_with_its_own_level(company, root):
    record = dataset(company)
    share = record_grant(company, OWNER, "dataset", record.id, "projects", root, level="read", actor=OWNER,
                         inherited=True, run_id="run-1")

    shown = project_share(view(record)["project_shares"], root)

    assert shown == {"project": root, "level": "read", "share_id": share.id, "inherited": True}


def test_a_credential_share_shows_its_own_level_share_id_and_inherited_flag(company, root):
    manual = credential(company)
    manual_share = record_grant(company, OWNER, "credential", manual.id, "projects", root, level="collaborator",
                                actor=OWNER)
    inherited = credential(company)
    inherited_share = record_grant(company, OWNER, "credential", inherited.id, "projects", root, level="read",
                                   actor=OWNER, inherited=True, run_id="run-2")

    grants = {grant["id"]: grant for grant in user_access.project_grants(company, MEMBER, root)}

    assert grants[manual.id]["level"] == "collaborator"
    assert grants[manual.id]["share_id"] == manual_share.id
    assert grants[manual.id]["inherited"] is False
    assert grants[inherited.id]["level"] == "read"
    assert grants[inherited.id]["share_id"] == inherited_share.id
    assert grants[inherited.id]["inherited"] is True


def test_a_queue_share_shows_its_own_level_and_share_id(company, root):
    queue = Queue(id=uuid4().hex, company=company, name="first", created=datetime.utcnow(), created_by=OWNER)
    queue.save()
    share = record_grant(company, OWNER, "queue", queue.id, "projects", root, level="read_write", actor=OWNER)

    call = FakeCall(Identity(user=OWNER, company=company, role=Role.user))
    svc.get_queues(call, company, GetQueuesRequest(project=root))

    assert call.result.data["queues"] == [
        {"id": queue.id, "name": "first", "level": "read_write", "share_id": share.id}
    ]
