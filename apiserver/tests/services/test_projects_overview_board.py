"""The overview board of a project: who reads it, who changes it, and what happens when two writers meet."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.projects import OverviewBoardRequest, SetOverviewBoardRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity
from apiserver.services import projects as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

CARDS = [{"id": "a", "type": "plot", "x": 0, "y": 0, "w": 3, "h": 12, "settings": {}}]


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def member(user, role):
    return ProjectMember(user=user, role=role, added=datetime.utcnow(), added_by="owner1")


@pytest.fixture
def project(company):
    project_id = f"board-{uuid4().hex[:8]}"
    Project(
        id=project_id, company=company, user="owner1", name=project_id, basename=project_id, created=datetime.utcnow(),
        visibility=ProjectVisibility.private,
        members=[member("collab", ProjectRole.collaborator), member("writer", ProjectRole.read_write),
                 member("viewer", ProjectRole.read)],
    ).save()
    return project_id


class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.requested_endpoint_version = PartialVersion("2.40")
        self.result = SimpleNamespace(data=None, data_model=None)


def as_user(company, user):
    return Identity(user=user, company=company, role=Role.user)


def get(company, project, user):
    call = FakeCall(as_user(company, user))
    svc.get_overview_board(call, company, OverviewBoardRequest(project=project))
    return call.result.data


def put(company, project, user, version, cards=CARDS):
    call = FakeCall(as_user(company, user))
    svc.set_overview_board(call, company, SetOverviewBoardRequest(project=project, version=version, cards=cards))
    return call.result.data


def test_a_project_that_never_saved_a_board_has_none(company, project):
    assert get(company, project, "viewer")["board"] is None


@pytest.mark.parametrize("user, can_edit", [("owner1", True), ("collab", False), ("writer", False), ("viewer", False)])
def test_the_answer_says_whether_the_caller_may_change_the_board(company, project, user, can_edit):
    assert get(company, project, user)["can_edit"] is can_edit


def test_a_company_admin_is_told_they_may_change_it(company, project):
    call = FakeCall(Identity(user="admin1", company=company, role=Role.admin))
    svc.get_overview_board(call, company, OverviewBoardRequest(project=project))
    assert call.result.data["can_edit"] is True


@pytest.mark.parametrize("user", ["owner1", "collab", "writer", "viewer"])
def test_every_member_reads_the_board(company, project, user):
    put(company, project, "owner1", 0)
    board = get(company, project, user)["board"]
    assert board["version"] == 1 and board["cards"] == CARDS


def test_the_owner_saves_and_the_version_goes_up(company, project):
    assert put(company, project, "owner1", 0) == {"version": 1}
    assert put(company, project, "owner1", 1) == {"version": 2}


@pytest.mark.parametrize("user", ["collab", "writer", "viewer"])
def test_nobody_but_the_owner_may_change_it(company, project, user):
    with pytest.raises(errors.forbidden.NoWritePermission):
        put(company, project, user, 0)
    assert get(company, project, "owner1")["board"] is None


def test_a_company_admin_passes_the_usual_owner_check(company, project):
    call = FakeCall(Identity(user="admin1", company=company, role=Role.admin))
    svc.set_overview_board(call, company, SetOverviewBoardRequest(project=project, version=0, cards=CARDS))
    assert call.result.data == {"version": 1}


def test_a_save_from_an_old_version_is_refused_and_the_newer_board_stays(company, project):
    put(company, project, "owner1", 0)
    newer = [dict(CARDS[0], id="newer")]
    put(company, project, "owner1", 1, newer)
    with pytest.raises(errors.bad_request.ValidationError) as caught:
        put(company, project, "owner1", 1)
    assert "changed by someone else" in str(caught.value)
    assert get(company, project, "owner1")["board"]["cards"] == newer


def test_an_invalid_board_is_refused_and_nothing_is_stored(company, project):
    with pytest.raises(errors.bad_request.ValidationError):
        put(company, project, "owner1", 0, [dict(CARDS[0], type="nonsense")])
    assert get(company, project, "owner1")["board"] is None


def test_a_stored_board_that_is_corrupt_reads_as_none(company, project):
    Project.objects(id=project).update(set__overview_board="{broken", set__overview_board_version=3)
    assert get(company, project, "owner1")["board"] is None


def test_a_stranger_does_not_even_learn_that_the_project_exists(company, project):
    with pytest.raises(errors.bad_request.InvalidProjectId):
        get(company, project, "stranger")
