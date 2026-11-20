"""users.delete (internal, system and root only): the user's profile and sign-in record go, and so do the records that
belong to the user, their credentials, their assignments of repositories, the shares granted to them and their
repository checks, and their membership of projects (with the shares they granted as a member); their devices are
revoked (and kept as audit history) and their queued and running jobs are stopped, also the running jobs of others on
their devices. Another user's records, also to a shared company credential, stay."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.users import UserRequest
from apiserver.bll import user as bll_user
from apiserver.bll.queue.queue_metrics import QueueMetrics
from apiserver.bll.task import TaskBLL
from apiserver.bll.user import UserBLL
from apiserver.database.model.audit import AuditEvent
from apiserver.database.model.auth import AuthUser
from apiserver.database.model.device import Device
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project, ProjectMember
from apiserver.database.model.queue import Entry, Queue
from apiserver.database.model.repository import Assignment, Repository, RepositoryCheck
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Execution, Task, TaskStatus
from apiserver.database.model.user import UserProfile
from apiserver.database.model.user_access import UserAccess
from apiserver.idp import git_credentials as store
from apiserver.idp.checks import AccessDenied, check_device
from apiserver.idp.tokens import issue_access_token, issue_job_token
from apiserver.idp.users import upsert_idp_user, user_id_for
from apiserver.service_repo import ServiceRepo
from apiserver.service_repo.auth.auth import check_idp_token
from apiserver.service_repo.validators import validate_role
from apiserver.services import users as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture(autouse=True)
def no_elasticsearch(monkeypatch):
    # stopping a task writes queue metrics to Elasticsearch, which these unit checks do not run
    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)


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
    for model in (
        Repository,
        Assignment,
        Share,
        Device,
        RepositoryCheck,
        UserAccess,
        UserProfile,
        AuthUser,
        Project,
        Queue,
        Task,
        Model,
    ):
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


def test_the_devices_of_the_user_are_revoked_and_kept(world):
    live_client = device(world, "leaving")
    live_agent = device(world, "leaving", kind="agent")
    already_revoked = device(world, "leaving", revoked=True)
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    for device_id in (live_client, live_agent, already_revoked):
        row = Device.objects(id=device_id).first()
        assert row is not None and row.revoked is True  # kept, not deleted
        # the sign-in record goes with the user, so the owner is unknown; a revoked device is refused first, for its
        # own reason
        with pytest.raises(AccessDenied, match="device revoked"):
            check_device(row, AuthUser.objects(id="leaving").first())


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


# --- the sign-in record, the project memberships and the jobs ---------------------------------------------------------


STOP_MESSAGE = "Stopped: its owner was deleted"
STOP_REASON = "owner deleted"


def make_project(world, id_, owner="admin", members=()):
    project = Project(
        id=id_,
        company=world.company,
        user=owner,
        name=f"Project {id_}",
        basename=f"Project {id_}",
        created=datetime.utcnow(),
        members=[ProjectMember(user=user, role="read", added=datetime.utcnow(), added_by=owner) for user in members],
    )
    project.save()
    return project.id


def members_of(project_id):
    return [member.user for member in Project.objects(id=project_id).first().members]


def make_queue(world, created_by="admin"):
    queue = Queue(
        id=uuid4().hex,
        company=world.company,
        name=f"queue-{uuid4().hex[:6]}",
        created=datetime.utcnow(),
        created_by=created_by,
    )
    queue.save()
    return queue.id


def queue_entries(queue_id):
    return [entry.task for entry in Queue.objects(id=queue_id).first().entries]


def make_task(
    world, owner, status=TaskStatus.created, queue=None, task_type="training", project="project-any", **fields
):
    """A task of the owner; one with a queue is also an entry of that queue."""
    task = Task(
        id=uuid4().hex,
        company=world.company,
        user=owner,
        project=project,
        name=f"task-{uuid4().hex[:6]}",
        type=task_type,
        created=datetime.utcnow(),
        status=status,
        started=datetime.utcnow() if status == TaskStatus.in_progress else None,
        execution=Execution(queue=queue) if queue else None,
        **fields,
    )
    task.save()
    if queue:
        Queue.objects(id=queue).update_one(push__entries=Entry(task=task.id, added=datetime.utcnow()))
    return task.id


def db_task(task_id):
    return Task.objects(id=task_id).first()


def project_share(world, project, resource, granted_by):
    share = Share(
        id=uuid4().hex,
        company=world.company,
        resource_type="queue",
        resource_id=resource,
        scope="projects",
        project=project,
        level="read",
        granted_by=granted_by,
        created=datetime.utcnow(),
    )
    share.save()
    return share.id


def test_the_sign_in_record_goes_with_the_profile_and_only_the_users_own(world):
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert UserProfile.objects(id="leaving").first() is None
    assert AuthUser.objects(id="leaving").first() is None
    assert UserProfile.objects(id="staying").first() is not None
    assert AuthUser.objects(id="staying").first() is not None
    assert AuthUser.objects(id="admin").first() is not None


def test_the_user_leaves_every_project_they_are_a_member_of(world):
    first = make_project(world, f"p1-{world.company}", members=("leaving", "staying"))
    second = make_project(world, f"p2-{world.company}", members=("staying", "leaving"))
    other = make_project(world, f"p3-{world.company}", members=("staying",))
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert members_of(first) == ["staying"]
    assert members_of(second) == ["staying"]
    assert members_of(other) == ["staying"]


def test_what_the_user_owns_keeps_naming_them_as_history(world):
    owned = make_project(world, f"owned-{world.company}", owner="leaving", members=("staying",))
    queue = make_queue(world, created_by="leaving")
    Repository(
        id=f"r2-{world.company}",
        company=world.company,
        key="github.com/acme/mine",
        host="github.com",
        path="acme/mine",
        name="mine",
        owner="leaving",
    ).save()
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert Project.objects(id=owned).first().user == "leaving"
    assert members_of(owned) == ["staying"]
    assert Queue.objects(id=queue).first().created_by == "leaving"
    assert Repository.objects(id=f"r2-{world.company}").first().owner == "leaving"


def test_the_queued_and_the_running_tasks_of_the_user_are_stopped_and_taken_out_of_the_queue(world):
    queue = make_queue(world)
    project = make_project(world, f"pj-{world.company}", members=("leaving",))  # a real project: a stop notifies it
    queued = make_task(world, "leaving", TaskStatus.queued, queue=queue, project=project)
    running = make_task(world, "leaving", TaskStatus.in_progress, project=project)
    svc.delete(delete_call(user="the-root"), None, UserRequest(user="leaving"))
    assert queue_entries(queue) == []
    for task_id in (queued, running):
        task = db_task(task_id)
        assert task.status == TaskStatus.stopped
        assert task.status_message == STOP_MESSAGE
        assert task.status_reason == STOP_REASON
        assert task.last_changed_by == "the-root"


def test_the_other_tasks_of_the_user_and_the_tasks_of_others_are_left_alone(world):
    queue = make_queue(world)
    own = {status: make_task(world, "leaving", status) for status in ("created", "completed", "stopped")}
    staying_queued = make_task(world, "staying", TaskStatus.queued, queue=queue)
    staying_running = make_task(world, "staying", TaskStatus.in_progress, last_device=device(world, "staying"))
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    for status, task_id in own.items():
        assert db_task(task_id).status == status
    assert db_task(staying_queued).status == TaskStatus.queued
    assert queue_entries(queue) == [staying_queued]
    assert db_task(staying_running).status == TaskStatus.in_progress


def test_a_running_task_of_another_user_on_the_users_agent_is_stopped_and_a_queued_one_stays(world):
    agent = device(world, "leaving", kind="agent")
    agent_queue = make_queue(world, created_by="leaving")
    running = make_task(world, "staying", TaskStatus.in_progress, last_device=agent)
    queued = make_task(world, "staying", TaskStatus.queued, queue=agent_queue)
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    task = db_task(running)
    assert (task.status, task.status_message, task.status_reason) == (TaskStatus.stopped, STOP_MESSAGE, STOP_REASON)
    assert db_task(queued).status == TaskStatus.queued
    assert queue_entries(agent_queue) == [queued]


def test_the_creator_of_models_datasets_and_tasks_of_other_types_stays_the_user(world):
    Model(
        id=f"m-{world.company}",
        company=world.company,
        user="leaving",
        name="model-one",
        created=datetime.utcnow(),
        ready=True,
    ).save()
    dataset = make_task(world, "leaving", TaskStatus.completed, task_type="data_processing")
    other = make_task(world, "leaving", TaskStatus.published, task_type="inference")
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert Model.objects(id=f"m-{world.company}").first().user == "leaving"
    assert db_task(dataset).user == "leaving"
    assert db_task(other).user == "leaving"


def test_the_shares_the_user_granted_as_a_member_go_with_the_membership_and_those_as_owner_stay(world):
    member_of = make_project(world, f"pm-{world.company}", members=("leaving", "staying"))
    owned = make_project(world, f"po-{world.company}", owner="leaving")
    by_leaving = project_share(world, member_of, "queue-1", "leaving")
    by_staying = project_share(world, member_of, "queue-2", "staying")
    as_owner = project_share(world, owned, "queue-3", "leaving")
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert {share.id for share in Share.objects(company=world.company)} == {by_staying, as_owner}
    assert by_leaving not in {share.id for share in Share.objects(company=world.company)}


def test_the_assignments_of_the_user_are_gone_and_those_of_a_remaining_member_stay(world):
    project = make_project(world, f"pa-{world.company}", members=("leaving", "staying"))
    assign(world, "leaving", world.shared)
    assign(world, "staying", world.shared)
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert members_of(project) == ["staying"]
    assert not Assignment.objects(company=world.company, user="leaving")
    assert Assignment.objects(company=world.company, user="staying").count() == 1


@pytest.fixture
def credentials_of_staying(world):
    """A web, a device and a job token of a user who stays: they show the helpers mint what the request path accepts."""
    user = SimpleNamespace(id="staying", company=world.company, role="user", name="Staying")
    agent = device(world, "staying", kind="agent")
    task = make_task(world, "staying", TaskStatus.in_progress)
    return {
        "web": issue_access_token("staying", world.company, "", kind="web", role="user"),
        "device": issue_access_token("staying", world.company, agent, kind="agent", role="user"),
        "job": issue_job_token(user, task, agent),
    }


@pytest.mark.parametrize("leg", ["web", "device", "job"])
def test_the_tokens_of_a_user_who_stays_are_accepted(credentials_of_staying, leg):
    check_idp_token(credentials_of_staying[leg])  # no error: the control of the refusals below


@pytest.mark.parametrize(
    "leg, message",
    [("web", "user disabled or unknown"), ("device", "unknown owner"), ("job", "owner disabled or unknown")],
)
def test_the_session_device_and_job_credentials_of_the_deleted_user_are_refused(world, leg, message):
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    # a live device row and a running task that exist after the delete, so that no other check refuses first
    agent = device(world, "leaving", kind="agent")
    if leg == "web":
        jwt = issue_access_token("leaving", world.company, "", kind="web", role="user")
    elif leg == "device":
        jwt = issue_access_token("leaving", world.company, agent, kind="agent", role="user")
    else:
        user = SimpleNamespace(id="leaving", company=world.company, role="user", name="Leaving")
        jwt = issue_job_token(user, make_task(world, "leaving", TaskStatus.in_progress), agent)
    with pytest.raises(errors.unauthorized.InvalidToken, match=message):
        check_idp_token(jwt)


def test_an_account_made_for_the_same_identity_afterwards_is_new_and_empty(world, monkeypatch):
    monkeypatch.setattr("apiserver.idp.users.get_default_company", lambda: world.company)
    issuer, subject = f"https://idp.test/{uuid4().hex}", uuid4().hex
    old = upsert_idp_user(issuer, subject, "Old Name", [], "cairnml-admin")
    user = old.id
    assert user == user_id_for(issuer, subject)
    long_ago = datetime.utcnow() - timedelta(days=30)
    AuthUser.objects(id=user).update(set__created=long_ago, set__validated=long_ago)
    project = make_project(world, f"pn-{world.company}", members=(user, "staying"))
    device(world, user)
    queue_share(world, "queue-1", user)
    assign(world, user, world.shared)
    UserAccess(
        id=f"own-{world.company}",
        company=world.company,
        user=user,
        kind="app",
        app_id="1",
        label="own",
        scope="user",
        host="github.com",
        secret=store.encrypt("pem", store.key_material()),
        created=datetime.utcnow(),
    ).save()

    svc.delete(delete_call(), None, UserRequest(user=user))
    again = upsert_idp_user(issuer, subject, "New Name", [], "cairnml-admin")

    assert (
        again.id == user
    )  # the id comes from the identity: the person owns their old projects, queues and repositories again
    assert again.created > long_ago and again.validated is None
    assert UserProfile.objects(id=user).first().name == "New Name"
    assert members_of(project) == ["staying"]
    assert not Device.objects(company=world.company, user=user, revoked=False)
    assert not UserAccess.objects(company=world.company, user=user)
    assert not Assignment.objects(company=world.company, user=user)
    assert not Share.objects(company=world.company, user=user)


def test_a_delete_that_failed_halfway_can_be_repeated(world, monkeypatch):
    project = make_project(world, f"pr-{world.company}", members=("leaving", "staying"))
    running = make_task(world, "leaving", TaskStatus.in_progress)
    live = device(world, "leaving")
    real = UserBLL._drop_profile
    calls = []

    def fail_once(company, user):
        calls.append(user)
        if len(calls) == 1:
            raise RuntimeError("the database went away")
        return real(company, user)

    monkeypatch.setattr(UserBLL, "_drop_profile", staticmethod(fail_once))
    with pytest.raises(RuntimeError):
        svc.delete(delete_call(user="the-root"), None, UserRequest(user="leaving"))
    # the first call got as far as the profile: the sign-in record, the membership and the job are done, and the profile
    # is the anchor of the retry
    assert AuthUser.objects(id="leaving").first() is None
    assert members_of(project) == ["staying"]
    assert db_task(running).status == TaskStatus.stopped
    assert UserProfile.objects(id="leaving").first() is not None
    assert not AuditEvent.objects(company=world.company, action="user.deleted")

    svc.delete(delete_call(user="the-root"), None, UserRequest(user="leaving"))

    assert UserProfile.objects(id="leaving").first() is None
    assert AuthUser.objects(id="leaving").first() is None
    assert db_task(running).status == TaskStatus.stopped
    assert Device.objects(id=live).first().revoked is True
    assert [event.actor for event in AuditEvent.objects(company=world.company, action="user.deleted")] == ["the-root"]
    assert [event.detail["device"] for event in revoke_events(world)] == [live]  # not audited twice
    with pytest.raises(errors.bad_request.InvalidUserId):  # now there is nothing left to delete
        svc.delete(delete_call(), None, UserRequest(user="leaving"))


def test_a_task_that_moved_on_before_it_was_stopped_is_skipped_and_the_delete_finishes(world, monkeypatch):
    first = make_task(world, "leaving", TaskStatus.in_progress)
    second = make_task(world, "leaving", TaskStatus.in_progress)
    real = TaskBLL.dequeue_and_change_status
    flipped = []

    def flip_the_first(task, *args, **kwargs):
        if not flipped:  # the task finishes between the query and the stop
            flipped.append(task.id)
            Task.objects(id=task.id).update(set__status=TaskStatus.completed)
        return real(task, *args, **kwargs)

    monkeypatch.setattr(TaskBLL, "dequeue_and_change_status", staticmethod(flip_the_first))
    svc.delete(delete_call(), None, UserRequest(user="leaving"))
    assert db_task(flipped[0]).status == TaskStatus.completed  # skipped, not stopped
    other = second if flipped[0] == first else first
    assert db_task(other).status == TaskStatus.stopped
    assert UserProfile.objects(id="leaving").first() is None
    assert AuditEvent.objects(company=world.company, action="user.deleted").first().detail["tasks_stopped"] == 1


def test_the_deletion_is_audited_once_with_the_real_counts_after_the_profile_is_gone(world, monkeypatch):
    make_project(world, f"pu1-{world.company}", members=("leaving", "staying"))
    make_project(world, f"pu2-{world.company}", members=("leaving",))
    queue = make_queue(world)
    make_task(world, "leaving", TaskStatus.queued, queue=queue)
    make_task(world, "leaving", TaskStatus.in_progress)
    make_task(world, "leaving", TaskStatus.completed)  # not stopped, not counted
    profile_present_at_audit = []
    real_audit = bll_user.audit

    def watching_audit(actor, action, *args, **kwargs):
        if action == "user.deleted":
            profile_present_at_audit.append(UserProfile.objects(id="leaving").first() is not None)
        return real_audit(actor, action, *args, **kwargs)

    monkeypatch.setattr(bll_user, "audit", watching_audit)
    svc.delete(delete_call(user="the-root"), None, UserRequest(user="leaving"))
    events = list(AuditEvent.objects(company=world.company, action="user.deleted"))
    assert len(events) == 1
    assert events[0].actor == "the-root"
    assert events[0].company == world.company
    assert events[0].detail == {"user": "leaving", "memberships_removed": 2, "tasks_stopped": 2}
    assert profile_present_at_audit == [False]  # written after the last record went
