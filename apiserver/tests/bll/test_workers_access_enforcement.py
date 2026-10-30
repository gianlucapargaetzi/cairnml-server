# apiserver/tests/bll/test_workers_access_enforcement.py
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.apimodels.workers import WorkerEntry, IdNameEntry
from apiserver.bll.workers import WorkerBLL
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity
from apiserver.tests.db import ensure_db

ensure_db()

worker_bll = WorkerBLL()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_root(company, id_="root1", owner="owner1", visibility=ProjectVisibility.private, members=()):
    project = Project(
        id=id_, company=company, user=owner, name=id_, basename=id_,
        created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_task(company, project, owner, id_="task1"):
    task = Task(id=id_, company=company, user=owner, name=id_, project=project, type="training", created=datetime.utcnow())
    task.save()
    return task


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


def register_worker(company, task_id, worker_id):
    entry = WorkerEntry(
        key=worker_bll._get_worker_key(company, "owner1", worker_id),
        id=worker_id,
        user=IdNameEntry(id="owner1", name="owner1"),
        company=IdNameEntry(id=company, name=company),
        task=IdNameEntry(id=task_id, name="task1"),
        project=IdNameEntry(id="root1", name="root1"),
        register_time=datetime.utcnow(),
        register_timeout=600,
        last_activity_time=datetime.utcnow(),
    )
    worker_bll._save_worker(entry)
    return entry


@pytest.mark.needs_redis
def test_get_all_redacts_a_private_tasks_name_from_a_stranger(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    register_worker(company, "task1", f"w-{uuid4().hex[:8]}")

    workers = worker_bll.get_all_with_projection(
        company, last_seen=3600, identity=identity(company, "stranger")
    )
    assert len(workers) == 1
    assert workers[0].task is None
    assert workers[0].project is None


@pytest.mark.needs_redis
def test_get_all_shows_the_task_to_a_member(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    register_worker(company, "task1", f"w-{uuid4().hex[:8]}")

    workers = worker_bll.get_all_with_projection(
        company, last_seen=3600, identity=identity(company, "u2")
    )
    assert len(workers) == 1
    assert workers[0].task is not None
    assert workers[0].task.id == "task1"
