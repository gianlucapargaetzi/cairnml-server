# apiserver/tests/services/test_notices.py
"""The notices of inherited datasets: access.notices and access.dismiss_notice. A user sees the notice of a run they ran,
while they still have use on the project; a dismissed notice is not returned again; a notice of a project the user has
left is not shown."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import access as access_bll
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.service_repo.auth import Identity
from apiserver.services import notices as svc
from apiserver.utilities.partial_version import PartialVersion
from apiserver.tests.db import ensure_db

ensure_db()

OWNER = "owner1"
RUNNER = "runner"
OTHER = "other"
ROOT = "root1"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data):
        self.identity = ident
        self.data = data
        self.requested_endpoint_version = PartialVersion("2.40")
        self.result = SimpleNamespace(data=None, data_model=None)


def notices_of(company, user):
    call = FakeCall(identity(company, user), {})
    svc.notices(call, company, None)
    return call.result.data["notices"]


def dismiss(company, user, share_id):
    svc.dismiss_notice(FakeCall(identity(company, user), {"id": share_id}), company, None)


def make_run(company, user):
    task = Task(id=uuid4().hex, company=company, user=user, name="train", type="training", created=datetime.utcnow(),
                started=datetime.utcnow(), status=TaskStatus.in_progress, project=ROOT)
    task.save()
    return task


def make_dataset(company, name):
    dataset = Dataset(id=uuid4().hex, company=company, kind="repo", ref=f"org/{name}", name=name, owner=OWNER,
                      visibility="user", state="ok", created=datetime.utcnow())
    dataset.save()
    return dataset


def inherit(company, dataset, run):
    """The inherited share a finished run makes (bll/dataset_inheritance), granted by the root project's owner."""
    return access_bll.record_grant(company, OWNER, "dataset", dataset.id, "projects", ROOT, level="read",
                                   actor=run.user, inherited=True, run_id=run.id)


@pytest.fixture
def setup(company):
    for user in (OWNER, RUNNER, OTHER):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    Project(id=ROOT, company=company, user=OWNER, name="Root project", basename=ROOT, created=datetime.utcnow(),
            members=[ProjectMember(user=RUNNER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER),
                     ProjectMember(user=OTHER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER)]).save()
    return company


def test_a_user_sees_the_notice_of_a_run_they_ran(company, setup):
    dataset = make_dataset(company, "images")
    run = make_run(company, RUNNER)
    share = inherit(company, dataset, run)

    notices = notices_of(company, RUNNER)

    assert len(notices) == 1
    notice = notices[0]
    assert notice["id"] == share.id
    assert notice["run_id"] == run.id
    assert notice["dataset"] == "images"
    assert notice["project"] == "Root project"
    assert notice["created"] == Share.objects(company=company, id=share.id).first().created.isoformat()


def test_a_user_does_not_see_the_notice_of_another_users_run(company, setup):
    dataset = make_dataset(company, "images")
    inherit(company, dataset, make_run(company, RUNNER))

    assert notices_of(company, OTHER) == []
    assert notices_of(company, OWNER) == []


def test_a_dismissed_notice_is_not_returned_again(company, setup):
    run = make_run(company, RUNNER)
    kept = inherit(company, make_dataset(company, "images"), run)
    dismissed = inherit(company, make_dataset(company, "labels"), run)

    dismiss(company, RUNNER, dismissed.id)

    assert [notice["id"] for notice in notices_of(company, RUNNER)] == [kept.id]
    assert Share.objects(company=company, id=dismissed.id).first().dismissed_by == [RUNNER]


def test_dismissing_twice_is_harmless(company, setup):
    share = inherit(company, make_dataset(company, "images"), make_run(company, RUNNER))

    dismiss(company, RUNNER, share.id)
    dismiss(company, RUNNER, share.id)

    assert Share.objects(company=company, id=share.id).first().dismissed_by == [RUNNER]


def test_dismissing_a_notice_the_user_may_not_see_is_not_found(company, setup):
    share = inherit(company, make_dataset(company, "images"), make_run(company, RUNNER))

    with pytest.raises(errors.bad_request.ValidationError) as other:
        dismiss(company, OTHER, share.id)
    with pytest.raises(errors.bad_request.ValidationError) as missing:
        dismiss(company, RUNNER, uuid4().hex)

    assert str(other.value) == str(missing.value)
    assert Share.objects(company=company, id=share.id).first().dismissed_by == []


def test_a_notice_of_a_project_the_user_has_left_is_not_shown(company, setup):
    share = inherit(company, make_dataset(company, "images"), make_run(company, RUNNER))
    project = Project.objects(company=company, id=ROOT).first()
    project.members = [member for member in project.members if member.user != RUNNER]
    project.save()

    assert notices_of(company, RUNNER) == []
    with pytest.raises(errors.bad_request.ValidationError):
        dismiss(company, RUNNER, share.id)
    assert Share.objects(company=company, id=share.id).count() == 1


def test_a_share_that_is_not_inherited_is_not_a_notice(company, setup):
    make_run(company, RUNNER)
    dataset = make_dataset(company, "images")
    access_bll.record_grant(company, OWNER, "dataset", dataset.id, "projects", ROOT, level="read", actor=OWNER)

    assert notices_of(company, RUNNER) == []
