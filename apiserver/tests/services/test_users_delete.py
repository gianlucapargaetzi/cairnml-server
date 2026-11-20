"""users.delete (internal, system and root only): the user's profile goes, and so do the records that belong to the user, their
credentials and their assignments of repositories. Another user's assignments, also to a shared company credential, stay."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.users import UserRequest
from apiserver.database.model.audit import AuditEvent
from apiserver.database.model.auth import AuthUser
from apiserver.database.model.repository import Assignment, Repository
from apiserver.database.model.user import UserProfile
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.service_repo import ServiceRepo
from apiserver.service_repo.validators import validate_role
from apiserver.services import users as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def world(monkeypatch):
    company = f"c-usr-{uuid4().hex[:8]}"
    now = datetime.utcnow()
    for user, role in (("admin", "admin"), ("leaving", "user"), ("staying", "user")):
        AuthUser(id=user, name=user.title(), company=company, role=role, created=now).save()
        UserProfile(id=user, company=company, name=user.title(), created=now).save()
    monkeypatch.setattr(store, "key_material", lambda: "test-key-material")
    repository = Repository(
        id=f"r-{company}",
        company=company,
        key="github.com/acme/proj",
        host="github.com",
        path="acme/proj",
        name="proj",
        owner="admin",
    ).save()
    for entry_id, owner, scope in (
        (f"leaving-{company}", "leaving", "user"),
        (f"company-{company}", "admin", "company"),
    ):
        UserAccess(
            id=entry_id,
            company=company,
            user=owner,
            kind="app",
            app_id="1",
            label=entry_id,
            scope=scope,
            host="github.com",
            secret=store.encrypt("pem", store.key_material()),
            created=now,
        ).save()
    state = SimpleNamespace(
        company=company, repository=repository.id, own=f"leaving-{company}", shared=f"company-{company}"
    )
    yield state
    for model in (Repository, Assignment, UserAccess, UserProfile, AuthUser):
        model.objects(company=company).delete()
    AuditEvent.objects(company=company).delete()


def assign(world, user, credential):
    Assignment(
        id=uuid4().hex, company=world.company, repository=world.repository, user=user, credential=credential
    ).save()


def delete_call(role="system", user="system-user"):
    return SimpleNamespace(identity=SimpleNamespace(user=user, role=role, company="system-company"))


@pytest.mark.parametrize("role", ["system", "root"])
def test_the_system_and_the_root_role_may_call_it(role):
    endpoint = ServiceRepo._get_endpoint("users.delete", ServiceRepo.max_endpoint_version())
    call = SimpleNamespace(identity=SimpleNamespace(role=role), auth=SimpleNamespace(token_kind="client"))
    validate_role(endpoint, call)


@pytest.mark.parametrize("role", ["admin", "user"])
def test_no_other_role_may_call_it(role):
    endpoint = ServiceRepo._get_endpoint("users.delete", ServiceRepo.max_endpoint_version())
    call = SimpleNamespace(identity=SimpleNamespace(role=role), auth=SimpleNamespace(token_kind="client"))
    with pytest.raises(errors.forbidden.RoleNotAllowed):
        validate_role(endpoint, call)


def test_the_call_removes_the_users_records_and_keeps_everyone_elses(world):
    assign(world, "leaving", world.shared)
    assign(world, "staying", world.shared)
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert not UserProfile.objects(id="leaving").first()
    assert UserProfile.objects(id="staying").first() is not None
    assert not UserAccess.objects(id=world.own).first()
    assert not Assignment.objects(company=world.company, user="leaving")
    assert UserAccess.objects(id=world.shared).first() is not None
    assert Assignment.objects(company=world.company, user="staying", credential=world.shared).count() == 1


def test_the_secret_of_the_users_credential_is_no_longer_stored(world):
    secret = UserAccess.objects(id=world.own).first().secret
    collection = UserAccess._get_collection()
    assert collection.count_documents({"secret": secret}) == 1
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert collection.count_documents({"secret": secret}) == 0


def test_the_audit_names_who_deleted(world):
    svc.delete(delete_call(user="the-root"), None, UserRequest(user="leaving"))
    events = AuditEvent.objects(company=world.company, action="user_access.deleted")
    assert [(event.actor, event.detail["owner"]) for event in events] == [("the-root", "leaving")]


def test_an_unknown_user_is_refused_and_nothing_changes(world):
    with pytest.raises(errors.bad_request.InvalidUserId):
        svc.delete(delete_call(), None, UserRequest(user="nobody"))
    assert UserAccess.objects(id=world.own).first() is not None
