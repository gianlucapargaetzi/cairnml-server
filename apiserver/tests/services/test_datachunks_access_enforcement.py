# apiserver/tests/services/test_datachunks_access_enforcement.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.datachunks import PrepareDownloadRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.datachunk import Datachunk, DatachunkKind, DatachunkState
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity
from apiserver.utilities.partial_version import PartialVersion
from apiserver.services import datachunks as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_root(company, id_="root1", owner="owner1", visibility=ProjectVisibility.private):
    project = Project(
        id=id_, company=company, user=owner, name=id_, basename=id_,
        created=datetime.utcnow(), visibility=visibility,
    )
    project.save()
    return project


def make_task(company, project, owner, id_="task1"):
    task = Task(id=id_, company=company, user=owner, name=id_, project=project, type="training", created=datetime.utcnow())
    task.save()
    return task


def make_stored_chunk(company, task, id_="chunk1"):
    chunk = Datachunk(
        id=id_, company=company, task=task, kind=DatachunkKind.artifact, bucket="b", key="k",
        name="art", size=1, sha256="0" * 64, state=DatachunkState.stored, created=datetime.utcnow(),
    )
    chunk.save()
    return chunk


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.authorization = None
        self.result = SimpleNamespace(data=None, data_model=None)


def test_prepare_download_refuses_a_task_in_a_private_project_to_a_stranger(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    make_stored_chunk(company, "task1")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.prepare_download(
            FakeCall(identity(company, "stranger")),
            company,
            PrepareDownloadRequest(id="chunk1"),
        )
