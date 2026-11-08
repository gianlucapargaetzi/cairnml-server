# apiserver/tests/bll/test_revocation.py
"""Revoking a dataset or a credential share of a project (R34, decision 5 of the design). A project's owner and
collaborators may revoke a dataset or credential share on their project, the dataset's and the credential's owner may
revoke their shares, and a plain project member may not. Revoking a dataset also removes its inherited credential share
when no other dataset shared with the project still needs that credential. A revoked inherited share is made again by
the next run that uses the dataset."""
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import access, user_access
from apiserver.bll.task.utils import ChangeStatusRequest
from apiserver.database.model.auth import User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import ParamsItem, Task, TaskStatus
from apiserver.database.model.user_access import UserAccess
from apiserver.tests.db import ensure_db

ensure_db()

SECTION = "Datasets"
RUNNER = "runner"
OWNER = "owner1"
COLLAB = "collab"
MEMBER = "member"
DS_OWNER = "dsowner"
CRED_OWNER = "credowner"
STRANGER = "stranger"
ROOT = "root1"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def setup(company):
    for user in (RUNNER, OWNER, COLLAB, MEMBER, DS_OWNER, CRED_OWNER, STRANGER):
        User(id=user, name=user, company=company, created=datetime.utcnow()).save()
    Project(id=ROOT, company=company, user=OWNER, name=ROOT, basename=ROOT, created=datetime.utcnow(), members=[
        ProjectMember(user=RUNNER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER),
        ProjectMember(user=COLLAB, role=ProjectRole.collaborator, added=datetime.utcnow(), added_by=OWNER),
        ProjectMember(user=MEMBER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER),
    ]).save()
    return company


def make_dataset(company, owner, ref, credential=None):
    """A dataset every user can use (visibility Public, with its public share): the run's user may use it, so a run
    shares it with the project (bll/dataset_inheritance)."""
    dataset = Dataset(id=uuid4().hex, company=company, kind="repo", ref=ref, name=ref.split("/")[1], owner=owner,
                      visibility="Public", credential=credential, state="ok", created=datetime.utcnow())
    dataset.save()
    access.record_grant(company, owner, "dataset", dataset.id, "public", actor=owner)
    return dataset


def make_credential(company, user):
    entry = UserAccess(id=uuid4().hex, company=company, user=user, kind="token", label="token", host="github.com",
                       path="", secret="secret", created=datetime.utcnow())
    entry.save()
    return entry


def make_task(company, user, refs=()):
    hyperparams = {SECTION: {}}
    for alias, ref in refs:
        for key, value in (("repo", ref), ("kind", "local")):
            name = f"{alias}/{key}"
            hyperparams[SECTION][name] = ParamsItem(section=SECTION, name=name, value=value, type="str")
    task = Task(id=uuid4().hex, company=company, user=user, name="train", type="training", created=datetime.utcnow(),
                started=datetime.utcnow(), status=TaskStatus.in_progress, project=ROOT, hyperparams=hyperparams)
    task.save()
    return task


def finish(task, user):
    """The status change the server makes when a run completes, through the same path as the API."""
    ChangeStatusRequest(task=task, new_status=TaskStatus.completed, force=True, user_id=user).execute()


def run_uses(company, refs):
    task = make_task(company, RUNNER, refs=refs)
    finish(task, RUNNER)
    return task


def dataset_shares(company, dataset):
    return list(Share.objects(company=company, resource_type="dataset", resource_id=dataset.id, scope="projects",
                              project=ROOT))


def credential_shares(company, entry):
    return list(Share.objects(company=company, resource_type="credential", resource_id=entry.id, scope="projects",
                              project=ROOT))


def project_credential_ids(company, actor):
    return {grant["id"] for grant in user_access.project_grants(company, actor, ROOT)}


# --- the project's owner and collaborators revoke a dataset share ----------------------------------------------------

def test_the_project_owner_revokes_an_inherited_dataset_share(setup):
    company = setup
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars")
    run_uses(company, [("cars", "dsowner/cars")])
    [share] = dataset_shares(company, dataset)
    assert share.inherited is True

    access.unshare(company, OWNER, share.id)

    assert dataset_shares(company, dataset) == []


def test_a_project_collaborator_revokes_an_inherited_dataset_share(setup):
    company = setup
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars")
    run_uses(company, [("cars", "dsowner/cars")])
    [share] = dataset_shares(company, dataset)

    access.unshare(company, COLLAB, share.id)

    assert dataset_shares(company, dataset) == []


def test_a_plain_project_member_may_not_revoke_a_dataset_share(setup):
    company = setup
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars")
    run_uses(company, [("cars", "dsowner/cars")])
    [share] = dataset_shares(company, dataset)

    with pytest.raises(errors.forbidden.NoWritePermission):
        access.unshare(company, RUNNER, share.id)
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.unshare(company, MEMBER, share.id)

    assert dataset_shares(company, dataset) == [share]


def test_a_stranger_may_not_revoke_a_dataset_share(setup):
    company = setup
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars")
    run_uses(company, [("cars", "dsowner/cars")])
    [share] = dataset_shares(company, dataset)

    # The dataset is Public, so the stranger sees it and holds read: refused as forbidden, not as not found.
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.unshare(company, STRANGER, share.id)

    assert dataset_shares(company, dataset) == [share]


def test_the_dataset_owner_revokes_a_share_of_their_dataset(setup):
    company = setup
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars")
    share = access.share(company, DS_OWNER, "dataset", dataset.id, "projects", ROOT, "read")
    assert share.inherited is False

    access.unshare(company, DS_OWNER, share.id)

    assert dataset_shares(company, dataset) == []


def test_a_revoked_inherited_share_is_made_again_by_the_next_run(setup):
    company = setup
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars")
    run_uses(company, [("cars", "dsowner/cars")])
    [share] = dataset_shares(company, dataset)
    access.unshare(company, OWNER, share.id)

    second = run_uses(company, [("cars", "dsowner/cars")])

    [again] = dataset_shares(company, dataset)
    assert again.inherited is True
    assert again.run_id == second.id


# --- a dataset's credential when the dataset is revoked --------------------------------------------------------------

def test_revoking_a_dataset_removes_its_inherited_credential_no_other_dataset_needs(setup):
    company = setup
    entry = make_credential(company, RUNNER)
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars", credential=entry.id)
    run_uses(company, [("cars", "dsowner/cars")])
    [dataset_share] = dataset_shares(company, dataset)
    [credential_share] = credential_shares(company, entry)
    assert credential_share.inherited is True

    access.unshare(company, OWNER, dataset_share.id)

    assert credential_shares(company, entry) == []


def test_revoking_a_dataset_keeps_a_credential_another_shared_dataset_still_needs(setup):
    company = setup
    entry = make_credential(company, RUNNER)
    cars = make_dataset(company, DS_OWNER, "dsowner/cars", credential=entry.id)
    trucks = make_dataset(company, DS_OWNER, "dsowner/trucks", credential=entry.id)
    run_uses(company, [("cars", "dsowner/cars"), ("trucks", "dsowner/trucks")])
    [cars_share] = dataset_shares(company, cars)
    [trucks_share] = dataset_shares(company, trucks)

    access.unshare(company, OWNER, cars_share.id)

    assert len(credential_shares(company, entry)) == 1
    assert project_credential_ids(company, OWNER) == {entry.id}

    access.unshare(company, OWNER, trucks_share.id)

    assert credential_shares(company, entry) == []


def test_revoking_a_dataset_keeps_a_credential_shared_by_hand(setup):
    company = setup
    entry = make_credential(company, RUNNER)
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars", credential=entry.id)
    access.share(company, RUNNER, "credential", entry.id, "projects", ROOT, "read")
    run_uses(company, [("cars", "dsowner/cars")])
    [dataset_share] = dataset_shares(company, dataset)

    access.unshare(company, OWNER, dataset_share.id)

    [kept] = credential_shares(company, entry)
    assert kept.inherited is False


# --- the credential's owner, and the project's owner, revoke a credential share --------------------------------------

def test_the_credential_owner_revokes_a_credential_share(setup):
    company = setup
    entry = make_credential(company, CRED_OWNER)
    share = access.share(company, CRED_OWNER, "credential", entry.id, "projects", ROOT, "read")

    access.unshare(company, CRED_OWNER, share.id)

    assert credential_shares(company, entry) == []


def test_the_project_owner_revokes_an_inherited_credential_share(setup):
    company = setup
    entry = make_credential(company, RUNNER)
    make_dataset(company, DS_OWNER, "dsowner/cars", credential=entry.id)
    run_uses(company, [("cars", "dsowner/cars")])
    [share] = credential_shares(company, entry)

    access.unshare(company, OWNER, share.id)

    assert credential_shares(company, entry) == []


def test_a_plain_project_member_may_not_revoke_a_credential_share(setup):
    company = setup
    entry = make_credential(company, RUNNER)
    make_dataset(company, DS_OWNER, "dsowner/cars", credential=entry.id)
    run_uses(company, [("cars", "dsowner/cars")])
    [share] = credential_shares(company, entry)

    with pytest.raises(errors.forbidden.NoWritePermission):
        access.unshare(company, MEMBER, share.id)

    assert credential_shares(company, entry) == [share]


def test_revoking_a_credential_removes_it_from_the_project(setup):
    company = setup
    entry = make_credential(company, RUNNER)
    dataset = make_dataset(company, DS_OWNER, "dsowner/cars", credential=entry.id)
    run_uses(company, [("cars", "dsowner/cars")])
    assert project_credential_ids(company, OWNER) == {entry.id}
    [share] = credential_shares(company, entry)

    access.unshare(company, RUNNER, share.id)

    assert project_credential_ids(company, OWNER) == set()
    assert project_credential_ids(company, MEMBER) == set()
    # the dataset itself is still shared; it has no access through the credential any more
    assert len(dataset_shares(company, dataset)) == 1
