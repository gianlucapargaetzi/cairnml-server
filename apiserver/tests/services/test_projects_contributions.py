# apiserver/tests/services/test_projects_contributions.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.projects import ProjectContributionsRequest
from apiserver.bll.project.project_bll import ProjectBLL
from apiserver.database.model import EntityVisibility
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.database.model.user import User
from apiserver.service_repo.auth import Identity
from apiserver.services import projects as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

NOW = datetime(2026, 10, 30, 12, 0)


@pytest.fixture
def company():
    # Every test gets its own company so that data from other tests never interferes
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def ids():
    # Project and user ids are global in the database, so each test uses its own suffix
    suffix = uuid4().hex[:8]
    return SimpleNamespace(
        root=f"root-{suffix}",
        sub=f"sub-{suffix}",
        empty=f"empty-{suffix}",
        ua=f"ua-{suffix}",
        ub=f"ub-{suffix}",
        uc=f"uc-{suffix}",
    )


def make_project(company, id_, owner="owner1", parent=None, visibility=ProjectVisibility.public, members=()):
    project = Project(
        id=id_,
        company=company,
        user=owner,
        name=f"{parent.name}/{id_}" if parent else id_,
        basename=id_,
        created=datetime.utcnow(),
        visibility=visibility,
        parent=parent.id if parent else None,
        path=[*(parent.path or []), parent.id] if parent else [],
        members=[
            ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members
        ],
    )
    project.save()
    return project


def make_user(company, id_, name):
    User(id=id_, company=company, name=name).save()


def make_task(company, project_id, user, created, status=TaskStatus.completed, id_=None, system_tags=None):
    task = Task(
        id=id_ or f"t-{uuid4().hex[:12]}",
        company=company,
        user=user,
        project=project_id,
        name="run",
        type="training",
        created=created,
        status=status,
        system_tags=system_tags or [],
    )
    task.save()
    return task


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.requested_endpoint_version = PartialVersion("2.39")
        self.result = SimpleNamespace(data=None, data_model=None)


def test_counts_status_days_and_contributors_of_the_period(company, ids):
    make_project(company, ids.root)
    make_project(company, ids.sub, parent=Project.objects(id=ids.root).get())
    make_user(company, ids.ua, "Alice")
    make_user(company, ids.ub, "Bob")
    make_user(company, ids.uc, "Carol")

    make_task(company, ids.root, ids.ub, datetime(2026, 10, 27, 23, 0))  # before the period
    make_task(company, ids.root, ids.ua, datetime(2026, 10, 28, 8, 0))
    make_task(company, ids.root, ids.ub, datetime(2026, 10, 29, 9, 0), status=TaskStatus.failed)
    make_task(company, ids.root, ids.ua, datetime(2026, 10, 30, 10, 0), status=TaskStatus.in_progress)
    make_task(company, ids.root, ids.uc, datetime(2026, 10, 30, 11, 0), status=TaskStatus.queued)
    make_task(company, ids.root, ids.ua, datetime(2026, 10, 30, 11, 30), system_tags=[EntityVisibility.hidden.value])
    make_task(company, ids.sub, ids.ua, datetime(2026, 10, 29, 9, 30))  # sub-project runs count
    make_task(company, ids.root, ids.ua, datetime(2026, 10, 29, 9, 45), status=TaskStatus.published)
    make_task("other-company", ids.root, ids.ua, datetime(2026, 10, 29, 9, 0))  # not this company's
    make_task(company, ids.root, ids.ua, datetime(2026, 10, 29, 9, 50), status=TaskStatus.completed)

    result = ProjectBLL.get_contributions(company, ids.root, period_days=3, utc_offset_minutes=0, limit=2, now=NOW)

    assert result["period_days"] == 3
    # 7 runs: the one before the period, the hidden one and the other company's are left out
    assert result["total"] == 7
    assert result["completed"] == 3
    assert result["failed"] == 1
    assert result["running"] == 2
    assert result["days"] == [
        {"date": "2026-10-28", "runs": 1},
        {"date": "2026-10-29", "runs": 4},
        {"date": "2026-10-30", "runs": 2},
    ]
    assert result["contributor_count"] == 3
    assert result["contributors"] == [
        {"id": ids.ua, "name": "Alice", "runs": 5},
        {"id": ids.ub, "name": "Bob", "runs": 1},
    ]


def test_local_days_follow_the_callers_offset(company, ids):
    make_project(company, ids.root)
    make_user(company, ids.ua, "Zoe")
    make_task(company, ids.root, ids.ua, datetime(2026, 10, 27, 23, 0))

    # Two hours east of UTC the run happened on 2026-10-28 local time, which is inside a 3 day period
    outside = ProjectBLL.get_contributions(company, ids.root, period_days=3, utc_offset_minutes=0, limit=5, now=NOW)
    inside = ProjectBLL.get_contributions(company, ids.root, period_days=3, utc_offset_minutes=120, limit=5, now=NOW)

    assert outside["total"] == 0
    assert outside["days"] == []
    assert outside["contributors"] == []
    assert inside["total"] == 1
    assert inside["days"] == [{"date": "2026-10-28", "runs": 1}]


def test_empty_project_has_no_runs_and_no_contributors(company, ids):
    make_project(company, ids.empty)
    result = ProjectBLL.get_contributions(company, ids.empty, period_days=7, utc_offset_minutes=0, limit=5, now=NOW)
    assert result == {
        "period_days": 7,
        "total": 0,
        "completed": 0,
        "failed": 0,
        "running": 0,
        "days": [],
        "contributors": [],
        "contributor_count": 0,
    }


def test_a_stranger_cannot_read_the_contributions_of_a_private_project(company, ids):
    make_project(company, ids.root, visibility=ProjectVisibility.private)
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.get_contributions(
            FakeCall(identity(company, "stranger")),
            company,
            ProjectContributionsRequest(project=ids.root),
        )


def test_a_member_reads_the_contributions_and_gets_the_response_shape(company, ids):
    make_project(company, ids.root, visibility=ProjectVisibility.private, members=[("u2", ProjectRole.read)])
    call = FakeCall(identity(company, "u2"))
    svc.get_contributions(call, company, ProjectContributionsRequest(project=ids.root, period_days=30))
    assert set(call.result.data) == {
        "period_days", "total", "completed", "failed", "running", "days", "contributors", "contributor_count",
    }
    assert call.result.data["period_days"] == 30
