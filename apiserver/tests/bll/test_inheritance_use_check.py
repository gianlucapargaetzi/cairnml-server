# apiserver/tests/bll/test_inheritance_use_check.py
"""A finished run shares a dataset, and its credential, with its project only when the run's user can use the dataset.
A member of a project who names another user's private dataset in a run does not share that dataset or its credential."""
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.bll import dataset_inheritance
from apiserver.database.model.auth import User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import ParamsItem, Task, TaskStatus
from apiserver.database.model.user_access import UserAccess
from apiserver.tests.db import ensure_db

ensure_db()

SECTION = "Datasets"
MEMBER = "member_m"
OWNER = "owner_a"
ROOT = "root_p"


@pytest.fixture
def company():
    name = f"c-{uuid4().hex[:8]}"
    for user in (MEMBER, OWNER):
        User(id=user, name=user, company=name, created=datetime.utcnow()).save()
    Project(id=ROOT, company=name, user=OWNER, name=ROOT, basename=ROOT, created=datetime.utcnow(),
            members=[ProjectMember(user=MEMBER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER)]).save()
    yield name
    Share.objects(company=name).delete()
    Task.objects(company=name).delete()
    Dataset.objects(company=name).delete()
    UserAccess.objects(company=name).delete()
    Project.objects(company=name).delete()
    User.objects(company=name).delete()


def make_credential(company, user):
    entry = UserAccess(id=uuid4().hex, company=company, user=user, kind="app", label="token", host="github.com",
                       path="", secret="secret", created=datetime.utcnow())
    entry.save()
    return entry


def make_private_dataset(company, credential_id):
    """A dataset of A's, visible to A only: the member holds no share on it."""
    dataset = Dataset(id=uuid4().hex, company=company, kind="repo", ref="owner_a/secret", name="secret", owner=OWNER,
                      visibility="user", credential=credential_id, state="ok", created=datetime.utcnow())
    dataset.save()
    return dataset


def make_run(company, refs):
    hyperparams = {SECTION: {}}
    for alias, ref in refs:
        for key, value in (("repo", ref), ("kind", "local")):
            name = f"{alias}/{key}"
            hyperparams[SECTION][name] = ParamsItem(section=SECTION, name=name, value=value, type="str")
    task = Task(id=uuid4().hex, company=company, user=MEMBER, name="train", type="training", created=datetime.utcnow(),
                started=datetime.utcnow(), status=TaskStatus.in_progress, project=ROOT, hyperparams=hyperparams)
    task.save()
    return task


def test_a_member_run_does_not_share_a_private_dataset_it_cannot_use(company):
    dataset = make_private_dataset(company, None)
    run = make_run(company, [("secret", "owner_a/secret")])

    assert dataset_inheritance.publish_run_datasets(run) == []
    assert Share.objects(company=company, resource_type="dataset", resource_id=dataset.id, scope="projects").count() == 0


def test_a_member_run_does_not_share_the_credential_of_a_dataset_it_cannot_use(company):
    member_credential = make_credential(company, MEMBER)
    dataset = make_private_dataset(company, member_credential.id)
    run = make_run(company, [("secret", "owner_a/secret")])

    dataset_inheritance.publish_run_datasets(run)

    assert Share.objects(company=company, resource_type="credential", resource_id=member_credential.id,
                         scope="projects").count() == 0
    assert Share.objects(company=company, resource_type="dataset", resource_id=dataset.id, scope="projects").count() == 0


def test_a_run_completes_when_it_names_a_dataset_its_user_cannot_use(company):
    make_private_dataset(company, None)
    run = make_run(company, [("secret", "owner_a/secret")])

    from apiserver.bll.task.utils import ChangeStatusRequest
    ChangeStatusRequest(task=run, new_status=TaskStatus.completed, force=True, user_id=MEMBER).execute()

    assert Task.objects(id=run.id).first().status == TaskStatus.completed
