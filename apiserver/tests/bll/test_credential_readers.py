# apiserver/tests/bll/test_credential_readers.py
"""Who sees, uses and reads a credential: the readers of bll/user_access and the device token answers. The first tests
pin the behaviour the readers had before they read the share records (Task 10b). The grant writers keep the old field
and the share in step, so the same answers hold after the switch."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import user_access
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.services import devices
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")


@pytest.fixture(autouse=True)
def no_audit(monkeypatch):
    monkeypatch.setattr(devices, "audit", lambda *args, **kwargs: None)


def make_user(company, user_id, role=Role.user):
    User(id=user_id, name=user_id, company=company, role=role, created=datetime.utcnow()).save()


def make_root(company, id_, owner, members=()):
    Project(
        id=id_, company=company, user=owner, name=id_, basename=id_, created=datetime.utcnow(),
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    ).save()


@pytest.fixture(autouse=True)
def cleanup(company):
    """The company's rows go after each test: server_app() reads every company-scoped App, whatever the company."""
    yield
    UserAccess.objects(company=company).delete()
    Share.objects(company=company).delete()
    Project.objects(company=company).delete()
    User.objects(company=company).delete()


@pytest.fixture
def setup(company):
    """owner1 owns root1 and root2; u1 (the credential's owner) and u2 are members of root1; stranger is in no project;
    admin1 is a company admin in no project. acc1 is u1's token for github.com, and app1 is the company's GitHub App."""
    make_user(company, "owner1")
    make_user(company, "u1")
    make_user(company, "u2")
    make_user(company, "stranger")
    make_user(company, "admin1", role=Role.admin)
    make_root(company, "root1", "owner1", members=[("u1", ProjectRole.read), ("u2", ProjectRole.read)])
    make_root(company, "root2", "owner1")
    UserAccess(
        id="acc1", company=company, user="u1", kind="token", label="token", host="github.com", path="",
        secret=store.encrypt("ghp_secret", store.key_material()), created=datetime.utcnow(),
    ).save()
    UserAccess(
        id="app1", company=company, user="admin1", scope="company", kind="app", label="Server App", host="github.com",
        app_id="7", api_url="https://api.github.com", secret=store.encrypt("pem", store.key_material()),
        created=datetime.utcnow(),
    ).save()


def usable_by(company, user, project):
    return [entry.id for entry, _ in user_access.usable_for(company, user, "github.com", "owner/repo", project)]


def job_uses(company, enqueuer, root):
    return [entry.id for entry, _ in
            user_access.granted_for_job(company, enqueuer, root, "github.com", "acme/proj")]


def task_of(enqueuer, project="root1"):
    return SimpleNamespace(id="t1", project=project,
                           execution=SimpleNamespace(enqueued_by=enqueuer) if enqueuer else None)


def granted_token(company, enqueuer, project="root1"):
    return devices._granted_token(company, task_of(enqueuer, project), "dev_1", "t1", "127.0.0.1", "acme/proj")


# --- characterisation: the answers before the readers read the share records


def test_the_owner_sees_their_credential_and_everyone_sees_the_company_app(company, setup):
    own = user_access.credentials_for(company, "u1", False)
    assert [(row["id"], row["can_edit"]) for row in own] == [("acc1", True), ("app1", False)]

    other = user_access.credentials_for(company, "u2", False)
    assert [(row["id"], row["can_edit"], row["owner"]) for row in other] == [("app1", False, "Company")]

    admin = user_access.credentials_for(company, "admin1", True)
    assert [(row["id"], row["can_edit"]) for row in admin] == [("app1", True)]


def test_the_owner_sees_the_projects_the_credential_is_granted_to(company, setup):
    assert user_access.credentials_for(company, "u1", False)[0]["granted_projects"] == []

    user_access.grant(company, "u1", "acc1", "root1")

    assert user_access.list_for(company, "u1")["own"][0]["granted_projects"] == ["root1"]
    assert user_access.credentials_for(company, "u1", False)[0]["granted_projects"] == ["root1"]


def test_the_members_of_a_project_see_the_grants_made_to_it(company, setup):
    assert user_access.project_grants(company, "u2", "root1") == []

    user_access.grant(company, "u1", "acc1", "root1")

    grants = user_access.project_grants(company, "u2", "root1")
    assert [(g["id"], g["owner"], g["owner_active"], g["owner_is_member"]) for g in grants] == [
        ("acc1", "u1", True, True)]
    assert user_access.project_grants(company, "owner1", "root1")[0]["id"] == "acc1"
    with pytest.raises(errors.bad_request.ValidationError):
        user_access.project_grants(company, "stranger", "root1")


def test_a_member_of_the_project_may_use_a_granted_credential_and_nobody_else(company, setup):
    assert usable_by(company, "u2", "root1") == []

    user_access.grant(company, "u1", "acc1", "root1")

    assert usable_by(company, "u1", None) == ["acc1"]
    assert usable_by(company, "u1", "root1") == ["acc1"]
    assert usable_by(company, "u2", "root1") == ["acc1"]
    assert usable_by(company, "owner1", "root1") == ["acc1"]
    assert usable_by(company, "u2", None) == []
    assert usable_by(company, "u2", "root2") == []
    assert usable_by(company, "stranger", "root1") == []
    assert usable_by(company, "admin1", "root1") == []


def test_a_grant_ends_when_it_is_revoked(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    user_access.revoke(company, "u1", "acc1", "root1", is_admin=False)

    assert usable_by(company, "u2", "root1") == []
    assert usable_by(company, "u1", None) == ["acc1"]


def test_a_grant_ends_when_the_grantee_leaves_the_project(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    Project.objects(id="root1", company=company).update_one(pull__members__user="u2")

    assert usable_by(company, "u2", "root1") == []
    assert usable_by(company, "u1", "root1") == ["acc1"]


def test_a_grant_ends_when_its_owner_leaves_the_project(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    Project.objects(id="root1", company=company).update_one(pull__members__user="u1")

    assert usable_by(company, "u2", "root1") == []
    grants = user_access.project_grants(company, "owner1", "root1")
    assert [(g["id"], g["owner_is_member"]) for g in grants] == [("acc1", False)]


def test_a_job_of_a_member_uses_the_credential_granted_to_its_project(company, setup):
    assert job_uses(company, "u2", "root1") == []

    user_access.grant(company, "u1", "acc1", "root1")

    pairs = user_access.granted_for_job(company, "u2", "root1", "github.com", "acme/proj")
    assert [(entry.id, secret) for entry, secret in pairs] == [("acc1", "ghp_secret")]
    assert job_uses(company, "u1", "root1") == ["acc1"]
    assert job_uses(company, "stranger", "root1") == []
    assert job_uses(company, "admin1", "root1") == []
    assert job_uses(company, "u2", "root2") == []
    assert job_uses(company, "u2", None) == []
    assert user_access.granted_for_job(company, "u2", "root1", "gitlab.example", "acme/proj") == []


def test_a_task_of_the_project_gets_the_granted_token(company, setup):
    assert granted_token(company, "u2") is None

    user_access.grant(company, "u1", "acc1", "root1")

    answer = granted_token(company, "u2")
    assert answer["token"] == "ghp_secret"
    assert answer["username"] == "x-access-token"
    assert granted_token(company, "stranger") is None
    assert granted_token(company, None) is None


def test_a_task_gets_no_granted_token_when_the_project_turns_personal_tokens_off(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    Project.objects(id="root1", company=company).update_one(set__agent_tokens_allowed=False)

    assert granted_token(company, "u2") is None


def test_the_stored_git_token_is_the_company_one_for_the_repository(company, setup, monkeypatch):
    stored = SimpleNamespace(host="github.com", path="", username="x-access-token", secret="stored")
    monkeypatch.setattr(store, "find_credential", lambda company, repository: stored)
    monkeypatch.setattr(store, "secret_of", lambda entry: "stored-secret")

    answer = devices._stored_token(company, "dev_1", "t1", "https://github.com/acme/proj")

    assert answer == {"token": "stored-secret", "expires_at": None, "username": "x-access-token"}


def test_no_stored_git_token_is_refused(company, setup, monkeypatch):
    monkeypatch.setattr(store, "find_credential", lambda company, repository: None)

    with pytest.raises(errors.bad_request.ValidationError):
        devices._stored_token(company, "dev_1", "t1", "https://github.com/acme/proj")


# --- the readers read the share records: a share alone gives the use, and the old field alone gives nothing


def share_project(company, access_id, root, granted_by="u1"):
    Share(id=uuid4().hex, company=company, resource_type="credential", resource_id=access_id, scope="projects",
          project=root, user="", level="read", granted_by=granted_by, created=datetime.utcnow()).save()


def test_a_share_record_alone_gives_the_use_of_the_credential(company, setup):
    share_project(company, "acc1", "root1")

    assert usable_by(company, "u2", "root1") == ["acc1"]
    assert job_uses(company, "u2", "root1") == ["acc1"]
    assert granted_token(company, "u2")["token"] == "ghp_secret"
    assert [g["id"] for g in user_access.project_grants(company, "u2", "root1")] == ["acc1"]
    assert user_access.credentials_for(company, "u1", False)[0]["granted_projects"] == ["root1"]


def test_the_old_field_alone_gives_no_use_of_the_credential(company, setup):
    UserAccess.objects(company=company, id="acc1").update_one(push__granted_projects="root1")

    assert usable_by(company, "u2", "root1") == []
    assert job_uses(company, "u2", "root1") == []
    assert user_access.project_grants(company, "u2", "root1") == []


# --- R25: a deactivated owner or grantee gets no access through a credential reader


def deactivate(company, user_id):
    User.objects(id=user_id, company=company).update_one(set__idp_disabled=True)


def test_a_deactivated_owner_shares_no_use_of_the_credential(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    deactivate(company, "u1")

    assert usable_by(company, "u2", "root1") == []
    assert job_uses(company, "u2", "root1") == []
    assert granted_token(company, "u2") is None
    assert usable_by(company, "u1", None) == []


def test_a_deactivated_grantee_uses_no_credential_granted_to_the_project(company, setup):
    user_access.grant(company, "u1", "acc1", "root1")
    deactivate(company, "u2")

    assert usable_by(company, "u2", "root1") == []
    assert job_uses(company, "u2", "root1") == []
    assert granted_token(company, "u2") is None
    assert usable_by(company, "u1", "root1") == ["acc1"]
