# apiserver/tests/bll/task/test_get_task_with_write_access.py
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll.task.utils import get_task_with_write_access, get_many_tasks_for_writing
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity
from apiserver.tests.db import ensure_db

ensure_db()


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


def test_refuses_a_task_in_a_private_project_to_a_stranger(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        get_task_with_write_access("task1", company_id=company, identity=identity(company, "stranger"))


def test_refuses_even_with_a_restricted_projection(company):
    # project/user must decide access regardless of what the caller asked to load
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        get_task_with_write_access(
            "task1", company_id=company, identity=identity(company, "stranger"), only=("id",)
        )


def test_allows_a_read_write_member_to_write_their_own_task(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_task(company, "root1", "u2")
    task = get_task_with_write_access("task1", company_id=company, identity=identity(company, "u2"))
    assert task.id == "task1"


def test_get_many_for_writing_excludes_a_forbidden_task_when_not_throwing(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    result = get_many_tasks_for_writing(
        company_id=company,
        identity=identity(company, "stranger"),
        query=None,
        throw_on_forbidden=False,
    )
    assert "task1" not in {t.id for t in result}


def test_get_many_for_writing_raises_when_throwing(company):
    make_root(company)
    make_task(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        get_many_tasks_for_writing(
            company_id=company,
            identity=identity(company, "stranger"),
            query=None,
            throw_on_forbidden=True,
        )
