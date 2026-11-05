# apiserver/tests/bll/test_access_model_scenarios.py
"""Scenarios of the access model, each one a path a person or an agent takes end to end at unit level: an agent that
serves a queue on its owner's rights, a read user and a read_write user on a dataset, a run that shares its dataset and
credential with its project and the revocation of those shares, and a registry shared with a project that its job reads.
The parts of the scenario are the real BLL and service functions, with only GitHub, the storage and the request
plumbing replaced."""
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import DequeueRequest
from apiserver.bll import access
from apiserver.bll import datasets
from apiserver.bll import dataset_credentials
from apiserver.bll import user_access
from apiserver.bll.queue.queue_access import QueueAccessBLL
from apiserver.bll.queue.queue_metrics import QueueMetrics
from apiserver.bll.task.utils import ChangeStatusRequest
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.queue import Entry, Queue
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Execution, ParamsItem, Task, TaskStatus
from apiserver.database.model.user_access import UserAccess
from apiserver.service_repo.auth import Identity
from apiserver.service_repo.validators import _agent_refused
from apiserver.services import datasets as datasets_svc
from apiserver.services import devices
from apiserver.services import tasks as tasks_svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

SECTION = "Datasets"
REPO_URL = "https://github.com/acme/cars"
REGISTRY_URL = "https://github.com/owner/registry"
REGISTRY_REF = "owner/registry"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_user(company, user_id, role=Role.user, disabled=False):
    User(id=user_id, name=user_id, company=company, role=role, created=datetime.utcnow(),
         idp_disabled=disabled).save()


def make_root(company, id_, owner, members=()):
    Project(
        id=id_, company=company, user=owner, name=id_, basename=id_, created=datetime.utcnow(),
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner) for u, r in members],
    ).save()


def make_dataset(company, owner, ref, credential=None, kind="repo"):
    dataset = Dataset(id=uuid4().hex, company=company, kind=kind, ref=ref, name=ref.split("/")[1], owner=owner,
                      visibility="user", credential=credential, state="ok", created=datetime.utcnow())
    dataset.save()
    return dataset


def make_credential(company, user, kind="token"):
    entry = UserAccess(id=uuid4().hex, company=company, user=user, kind=kind, label="token", host="github.com",
                       path="", secret="secret", created=datetime.utcnow(), app_id="1", api_url=None)
    entry.save()
    return entry


def project_share(company, resource_type, resource_id, root, level, granted_by):
    return Share(id=uuid4().hex, company=company, resource_type=resource_type, resource_id=resource_id,
                 scope="projects", project=root, user="", level=level, granted_by=granted_by,
                 created=datetime.utcnow()).save()


def dataset_shares(company, dataset, root):
    return list(Share.objects(company=company, resource_type="dataset", resource_id=dataset.id, scope="projects",
                              project=root))


def credential_shares(company, entry, root):
    return list(Share.objects(company=company, resource_type="credential", resource_id=entry.id, scope="projects",
                              project=root))


def make_run(company, user, root, refs=(), status=TaskStatus.in_progress):
    hyperparams = {SECTION: {}}
    for alias, ref in refs:
        for key, value in (("repo", ref), ("kind", "local")):
            name = f"{alias}/{key}"
            hyperparams[SECTION][name] = ParamsItem(section=SECTION, name=name, value=value, type="str")
    task = Task(id=uuid4().hex, company=company, user=user, name="train", type="training", created=datetime.utcnow(),
                started=datetime.utcnow(), status=status, project=root, hyperparams=hyperparams)
    task.save()
    return task


def complete(task, user):
    ChangeStatusRequest(task=task, new_status=TaskStatus.completed, force=True, user_id=user).execute()


# --- 1. an agent with queue-only rights ---------------------------------------------------------------------------------

class FakeCall:
    def __init__(self, ident):
        self.identity = ident
        self.data = {}
        self.requested_endpoint_version = PartialVersion("2.38")
        self.result = SimpleNamespace(data=None, data_model=None)


class AgentCall:
    """A call made with an agent's token: the endpoint name is checked against the agent allowlist."""

    def __init__(self, ident):
        self.identity = ident
        self.auth = SimpleNamespace(token_kind="agent")


def test_agent_job_with_queue_only_rights(company, monkeypatch):
    monkeypatch.setattr(QueueMetrics, "log_queue_metrics_to_es", lambda self, company_id, queues: 0)
    # the owner is an admin, yet the agent carries the user role only: the queue is served on the owner's queue right
    make_user(company, "owner", role=Role.admin)
    queue = Queue(id=uuid4().hex, company=company, name=f"q-{uuid4().hex[:6]}", created=datetime.utcnow(),
                  public=False, system_tags=[], created_by="someone-else")
    queue.save()
    Share(id=uuid4().hex, company=company, resource_type="queue", resource_id=queue.id, scope="user", project="",
          user="owner", level="read", granted_by="someone-else", created=datetime.utcnow()).save()
    agent = Identity(user="owner", company=company, role=Role.user)

    QueueAccessBLL.assert_can_serve(agent, company, [queue.id])

    now = datetime.utcnow()
    Task(id="job-1", company=company, user="owner", name="job", type="training", created=now,
         status=TaskStatus.queued, execution=Execution(queue=queue.id)).save()
    Queue.objects(id=queue.id).update_one(push__entries=Entry(task="job-1", added=now))
    call = FakeCall(agent)
    tasks_svc.dequeue(call, company, DequeueRequest(task="job-1"))
    assert call.result.data_model.dequeued == 1
    assert Queue.objects(id=queue.id).first().entries == []

    # outside the agent allowlist: a queue administration call is refused for the agent, a dequeue is not
    assert _agent_refused(SimpleNamespace(name="queues.create"))
    assert _agent_refused(SimpleNamespace(name="projects.add_member"))
    assert not _agent_refused(SimpleNamespace(name="tasks.dequeue"))
    from apiserver.service_repo.validators import validate_role
    with pytest.raises(errors.forbidden.NoWritePermission):
        validate_role(SimpleNamespace(name="queues.create", authorize=False, allows=lambda role: True),
                      AgentCall(agent))


# --- 2. a read user downloads a dataset and cannot write it -------------------------------------------------------------

READ = {"contents": "read"}
WRITE = {"contents": "write"}


class FakeGitHubApp:
    def __init__(self):
        self.requests = []

    def installation_token(self, repo, permissions=None):
        permissions = permissions or READ
        self.requests.append((repo, permissions))
        return SimpleNamespace(token=f"tok-{permissions['contents']}", expires_at=datetime(2030, 1, 1))


def person_call(user):
    return SimpleNamespace(
        data={"repository": REPO_URL, "level": "read"},
        identity=SimpleNamespace(user=user, role="user"),
        auth=SimpleNamespace(token_kind="client", job_task=None),
        real_ip="127.0.0.1",
        get_header=lambda header, default=None: default,
        HEADER_REAL_IP="X-Real-IP",
    )


def test_read_user_downloads_dataset_and_cannot_write(company, monkeypatch):
    make_user(company, "owner1")
    make_user(company, "reader")
    make_root(company, "root1", "owner1", members=[("reader", ProjectRole.read)])
    dataset = make_dataset(company, "owner1", "acme/cars")
    project_share(company, "dataset", dataset.id, "root1", "read", "owner1")

    # the download: a read-only token for the dataset repository, from the datasets.git_token path
    github = FakeGitHubApp()
    monkeypatch.setattr(datasets_svc, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(datasets_svc, "client_address", lambda call: "127.0.0.1")
    monkeypatch.setattr(datasets_svc, "public_repo", lambda repo: False)
    monkeypatch.setattr(datasets_svc, "build_from_config", lambda: github)
    monkeypatch.setattr(datasets_svc.access_bll, "held_on_repository",
                        lambda company_, user, is_admin, repo: ("read", "dataset"))
    answer = datasets_svc.git_token(person_call("reader"), company, None)
    assert answer["token"] == "tok-read"
    assert github.requests == [("acme/cars", READ)]

    # the object-store keys: read is issued for the dataset's prefix, read_write is refused
    assert dataset_credentials._authorize(company, "reader", "acme/cars", "read", False) == \
        dataset_credentials.prefix_of(company, "acme/cars")
    with pytest.raises(errors.forbidden.NoWritePermission):
        dataset_credentials._authorize(company, "reader", "acme/cars", "read_write", False)


# --- 3. a read_write user writes but cannot share -----------------------------------------------------------------------

def test_read_write_user_writes_but_cannot_share(company):
    make_user(company, "owner1")
    make_user(company, "writer")
    make_root(company, "root1", "owner1", members=[("writer", ProjectRole.read)])
    dataset = make_dataset(company, "owner1", "acme/cars")
    project_share(company, "dataset", dataset.id, "root1", "read_write", "owner1")

    # the write: the keys for read_write are issued
    assert dataset_credentials._authorize(company, "writer", "acme/cars", "read_write", False)

    # the sharing: manage_sharing is needed on the dataset, which read_write does not give
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.share(company, "writer", "dataset", dataset.id, "projects", "root1", "read")
    with pytest.raises(errors.bad_request.ValidationError):
        datasets.set_visibility(company, "writer", dataset.id, "projects", ["root1"], False)
    with pytest.raises(errors.bad_request.ValidationError):
        datasets.allocate(company, "writer", dataset.id, "root1", False)
    [only] = dataset_shares(company, dataset, "root1")
    assert only.level == "read_write"
    assert only.granted_by == "owner1"


# --- 4. a run inherits its dataset and credential; revoking the dataset removes what it inherited -----------------------

def test_inheritance_and_revocation_removes_inherited_credential(company):
    make_user(company, "owner1")
    make_user(company, "runner")
    make_user(company, "dsowner")
    make_root(company, "root1", "owner1", members=[("runner", ProjectRole.read)])
    entry = make_credential(company, "runner")
    dataset = make_dataset(company, "dsowner", "dsowner/cars", credential=entry.id)

    task = make_run(company, "runner", "root1", refs=[("cars", "dsowner/cars")])
    complete(task, "runner")

    [dataset_share] = dataset_shares(company, dataset, "root1")
    assert dataset_share.inherited is True
    assert dataset_share.run_id == task.id
    [credential_share] = credential_shares(company, entry, "root1")
    assert credential_share.inherited is True
    assert credential_share.run_id == task.id
    assert credential_share.granted_by == "runner"

    # the project's owner revokes the dataset: its inherited credential goes with it
    access.unshare(company, "owner1", dataset_share.id)

    assert dataset_shares(company, dataset, "root1") == []
    assert credential_shares(company, entry, "root1") == []


def test_revocation_keeps_hand_made_credential(company):
    make_user(company, "owner1")
    make_user(company, "runner")
    make_user(company, "dsowner")
    make_root(company, "root1", "owner1", members=[("runner", ProjectRole.read)])
    entry = make_credential(company, "runner")
    dataset = make_dataset(company, "dsowner", "dsowner/cars", credential=entry.id)
    manual = access.share(company, "runner", "credential", entry.id, "projects", "root1", "read")

    task = make_run(company, "runner", "root1", refs=[("cars", "dsowner/cars")])
    complete(task, "runner")
    [dataset_share] = dataset_shares(company, dataset, "root1")

    access.unshare(company, "owner1", dataset_share.id)

    assert dataset_shares(company, dataset, "root1") == []
    [kept] = credential_shares(company, entry, "root1")
    assert kept.id == manual.id
    assert kept.inherited is False


# --- 5. a registry shared with a project gives its job the registry's credential ----------------------------------------

class FakeApp:
    def __init__(self, app_id, secret, api_url):
        self.secret = secret

    def installation_token(self, repo):
        return SimpleNamespace(token=f"tok-{self.secret}", expires_at=datetime(2030, 1, 1))


def test_registry_shared_with_project(company, monkeypatch):
    make_user(company, "owner1")
    make_user(company, "jobuser")
    make_root(company, "root1", "owner1", members=[("jobuser", ProjectRole.read)])
    entry = make_credential(company, "owner1", kind="app")
    registry = make_dataset(company, "owner1", REGISTRY_REF, credential=entry.id, kind="registry")
    access.share(company, "owner1", "credential", entry.id, "projects", "root1", "read")
    datasets.set_visibility(company, "owner1", registry.id, "projects", ["root1"], False)
    task = make_run(company, "jobuser", "root1")
    task.execution = Execution(enqueued_by="jobuser")
    task.save()

    monkeypatch.setattr(user_access, "_secret", lambda e: f"s-{e.id}")
    monkeypatch.setattr(devices, "GitHubApp", FakeApp)
    monkeypatch.setattr(devices, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(devices, "client_address", lambda call: "127.0.0.1")
    monkeypatch.setattr(devices, "_public_answer", lambda *args, **kwargs: None)
    monkeypatch.setattr(devices, "build_from_config", lambda: None)
    monkeypatch.setattr(devices, "Token", SimpleNamespace(decode=lambda _b: {"kind": "job", "task": task.id}))
    monkeypatch.setattr(devices, "_bearer", lambda call: "t")
    call = SimpleNamespace(data={}, identity=None, auth=None)
    request = SimpleNamespace(task=task.id, repository=REGISTRY_URL)

    answer = devices.git_token(call, company, request)
    assert answer["token"] == f"tok-s-{entry.id}"

    # the registry share is removed: the job is refused, and the refusal names the registry
    [registry_share] = Share.objects(company=company, resource_id=registry.id, scope="projects", project="root1")
    access.unshare(company, "owner1", registry_share.id)
    with pytest.raises(errors.bad_request.ValidationError) as refused:
        devices.git_token(call, company, request)
    assert "registry" in refused.value.msg
    assert REGISTRY_REF.split("/")[1] in refused.value.msg
