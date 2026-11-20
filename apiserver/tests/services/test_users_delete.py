"""users.delete (internal, system and root only): the user's profile goes, and so do the records that belong to the user, their
credentials, their assignments of repositories, the shares granted to them and their repository checks; their devices are
revoked (and kept as audit history). Another user's records, also to a shared company credential, stay."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.users import UserRequest
from apiserver.bll.user import UserBLL
from apiserver.database.model.audit import AuditEvent
from apiserver.database.model.auth import AuthUser
from apiserver.database.model.device import Device
from apiserver.database.model.repository import Assignment, Repository, RepositoryCheck
from apiserver.database.model.share import Share
from apiserver.database.model.user import UserProfile
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.checks import AccessDenied, check_device
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
    for model in (Repository, Assignment, Share, Device, RepositoryCheck, UserAccess, UserProfile, AuthUser):
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


# --- the shares granted to the user, their devices and their repository checks ---------------------------------------------------


def queue_share(world, resource, user, granted_by="admin"):
    """A user-scope share (a grant of one queue to one user, read only: the only kind of share a user scope allows)."""
    share = Share(
        id=uuid4().hex,
        company=world.company,
        resource_type="queue",
        resource_id=resource,
        scope="user",
        user=user,
        level="read",
        granted_by=granted_by,
        created=datetime.utcnow(),
    )
    share.save()
    return share.id


def device(world, user, kind="client", revoked=False):
    row = Device(
        id=uuid4().hex,
        user=user,
        company=world.company,
        kind=kind,
        name=f"{user}-{kind}",
        public_key="pem",
        created=datetime.utcnow(),
        revoked=revoked,
    )
    row.save()
    return row.id


def check_row(world, user):
    RepositoryCheck(
        id=uuid4().hex, company=world.company, repository=world.repository, user=user, checked=datetime.utcnow()
    ).save()


def revoke_events(world):
    return AuditEvent.objects(company=world.company, action="device.revoked")


def test_the_shares_granted_to_the_user_go_with_the_user(world):
    queue_share(world, "queue-1", "leaving")
    queue_share(world, "queue-2", "leaving")
    queue_share(world, "queue-1", "staying")
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert not Share.objects(company=world.company, user="leaving")
    assert Share.objects(company=world.company, user="staying").count() == 1


def test_the_shares_the_user_granted_to_others_stay(world):
    by_leaving = queue_share(world, "queue-1", "staying", granted_by="leaving")
    by_admin = queue_share(world, "queue-2", "staying")
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert {share.id for share in Share.objects(company=world.company)} == {by_leaving, by_admin}


def test_the_devices_of_the_user_are_revoked_and_kept_and_refused_for_a_token(world):
    live_client = device(world, "leaving")
    live_agent = device(world, "leaving", kind="agent")
    already_revoked = device(world, "leaving", revoked=True)
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    for device_id in (live_client, live_agent, already_revoked):
        # as service_repo/auth does on a device-token request: both records are loaded from the database
        row = Device.objects(id=device_id).first()
        owner = AuthUser.objects(id="leaving").first()  # the login record stays (users.delete does not remove it)
        assert owner is not None and not owner.idp_disabled
        with pytest.raises(AccessDenied, match="device revoked"):
            check_device(row, owner)
        assert row.revoked is True  # kept, not deleted


def test_the_devices_of_another_user_are_not_revoked(world):
    stays = device(world, "staying")
    device(world, "leaving")
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    row = Device.objects(id=stays).first()
    assert row.revoked is False
    check_device(row, AuthUser.objects(id="staying").first())  # still accepted


def test_each_newly_revoked_device_is_audited_with_the_actor_and_the_reason(world):
    live_client = device(world, "leaving")
    live_agent = device(world, "leaving", kind="agent")
    device(world, "leaving", revoked=True)  # revoked before: no second event
    device(world, "staying")
    svc.delete(delete_call(user="the-root"), None, UserRequest(user="leaving"))
    events = list(revoke_events(world))
    assert sorted(event.detail["device"] for event in events) == sorted([live_client, live_agent])
    assert {(event.actor, event.detail["reason"]) for event in events} == {("the-root", "owner deleted")}


def test_the_repository_checks_of_the_user_go_and_those_of_others_stay(world):
    check_row(world, "leaving")
    check_row(world, "staying")
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert [row.user for row in RepositoryCheck.objects(company=world.company)] == ["staying"]


def test_the_three_steps_can_be_repeated_without_a_second_effect(world):
    queue_share(world, "queue-1", "leaving")
    live = device(world, "leaving")
    check_row(world, "leaving")
    for _ in range(2):
        UserBLL._drop_shares(world.company, "leaving")
        UserBLL._revoke_devices(world.company, "leaving", "the-root")
        UserBLL._drop_repository_checks(world.company, "leaving")
        assert not Share.objects(company=world.company, user="leaving")
        assert Device.objects(id=live).first().revoked is True
        assert not RepositoryCheck.objects(company=world.company, user="leaving")
        assert [event.detail["device"] for event in revoke_events(world)] == [live]  # one event, not one per call
