# apiserver/tests/bll/test_queue_access_enforcement.py
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.bll.queue.queue_bll import QueueBLL
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.queue import Entry, Queue
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity
from apiserver.tests.db import ensure_db

ensure_db()

queue_bll = QueueBLL()


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
    task = Task(id=id_, company=company, user=owner, name="real-task-name", project=project, type="training", created=datetime.utcnow())
    task.save()
    return task


def make_queue(company, task_id, id_="queue1"):
    queue = Queue(
        id=id_, company=company, name=id_, created=datetime.utcnow(),
        entries=[Entry(task=task_id, added=datetime.utcnow())],
    )
    queue.save()
    return queue


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


def test_get_queue_infos_redacts_a_hidden_tasks_name_from_a_stranger(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    make_queue(company, "task1")

    [queue] = queue_bll.get_queue_infos(
        company_id=company, query_dict={}, identity=identity(company, "stranger")
    )
    assert queue["entries"][0]["task"]["name"] is None


def test_get_queue_infos_shows_the_name_to_a_member(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    make_task(company, "root1", "owner1")
    make_queue(company, "task1")

    [queue] = queue_bll.get_queue_infos(
        company_id=company, query_dict={}, identity=identity(company, "u2")
    )
    assert queue["entries"][0]["task"]["name"] == "real-task-name"
