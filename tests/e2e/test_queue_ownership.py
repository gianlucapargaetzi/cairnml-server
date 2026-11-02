"""Queue ownership, end to end: who creates, manages, uses and sees a queue, through the REST API as enrolled devices.

A queue belongs to the plain user who created it (the owner). Its owner manages it; a grantee uses it; a member of a
root project it is allocated to sees that project; an outsider sees and uses nothing of it; the company admin sees
every queue. A queue the caller may not use is reported as missing, and a refusal to manage it is a 403.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from support import client, workspace
from support.agent import worker_record
from support.api import Api, ApiError
from support.workspace import new_name

# The membership endpoints exist from this API version (services/projects.conf, 2.37); the queue metadata endpoint
# from 2.13. Every call of this module is made at this version.
QUEUE_API_VERSION = "2.37"
TASK_TYPE = "training"

OWNER, GRANTEE, MEMBER, OUTSIDER = "owner", "grantee", "member", "outsider"


@dataclass
class Person:
    label: str
    home: Path
    api: Api
    user_id: str


def _device_api(home: Path) -> Api:
    api = Api(client.device_id_of(home), client.key_path(home))
    api.version = QUEUE_API_VERSION
    return api


def _refused(api: Api, endpoint: str, body: dict) -> ApiError:
    with pytest.raises(ApiError) as refused:
        api.authed(endpoint, body)
    return refused.value


def _assert_forbidden(error: ApiError):
    assert error.status == 403, f"expected a 403 refusal, got {error}"


def _assert_no_queue(error: ApiError):
    """A queue the caller may not use: the same answer a genuinely missing id gets (400, 'Invalid queue id')."""
    assert error.status == 400, f"expected the missing-queue answer, got {error}"
    assert error.message.startswith("Invalid queue id"), f"expected the missing-queue answer, got {error}"


def _push(person: Person, project_id: str, label: str) -> str:
    return person.api.authed("tasks.create", {"name": new_name(label), "type": TASK_TYPE,
                                              "project": project_id})["id"]


def _allocate(owner: Person, project_id: str, queue_id: str):
    owner.api.authed("projects.add_queues", {"project": project_id, "queues": [queue_id]})


@pytest.fixture(scope="module")
def people(keycloak, tmp_path_factory):
    """One plain user per role, each enrolled on a machine of its own, removed afterwards."""
    accounts = {}
    enrolled = {}
    try:
        for label in (OWNER, GRANTEE, MEMBER, OUTSIDER):
            username = new_name(f"queue-{label}")
            password = os.urandom(12).hex()
            keycloak.create_user(username, password)
            accounts[label] = username
            home = tmp_path_factory.mktemp(f"queue-{label}")
            result = client.init(home, (username, password), name=f"e2e-{label}")
            assert result.returncode == 0, result.output
            api = _device_api(home)
            user_id = api.authed("users.get_current_user")["user"]["id"]
            enrolled[label] = Person(label, home, api, user_id)
        yield enrolled
    finally:
        for person in enrolled.values():
            client.logout(person.home)
        for username in accounts.values():
            keycloak.delete_user(username)


@pytest.fixture(scope="module")
def admin(client_home):
    return _device_api(client_home)


@pytest.fixture(scope="module")
def task_projects(people):
    """A root project of the owner and one of the grantee, where each of them creates the tasks it enqueues."""
    projects = {label: workspace.create_project(people[label].api, new_name(f"queue-{label}-tasks"))
                for label in (OWNER, GRANTEE)}
    yield projects
    for label, project_id in projects.items():
        workspace.delete_project(people[label].api, project_id)


@pytest.fixture
def make_queue(people):
    """Creates a queue as the owner. The queues are deleted afterwards, with whatever the test left queued in them."""
    created = []

    def make(label: str) -> str:
        queue_id = people[OWNER].api.authed("queues.create", {"name": new_name(f"queue-{label}")})["id"]
        created.append(queue_id)
        return queue_id

    yield make
    for queue_id in created:
        people[OWNER].api.authed("queues.delete", {"queue": queue_id, "force": True})


@pytest.fixture
def make_root(people):
    """Creates a root project as the owner, with the given members. The projects are deleted afterwards."""
    created = []

    def make(label: str, members=()) -> str:
        project_id = workspace.create_project(people[OWNER].api, new_name(f"queue-{label}"))
        created.append(project_id)
        for member_label, role in members:
            people[OWNER].api.authed("projects.add_member", {"project": project_id,
                                                             "user": people[member_label].user_id, "role": role})
        return project_id

    yield make
    for project_id in created:
        workspace.delete_project(people[OWNER].api, project_id)


def test_a_non_admin_creates_a_queue_and_it_is_recorded_as_theirs(people, make_queue):
    """A plain user creates a queue, and the queue records the user as its owner.

    Expect: the queue is created, and its created_by is the id of the user who created it.
    """
    queue_id = make_queue("created")
    queue = people[OWNER].api.authed("queues.get_by_id", {"queue": queue_id})["queue"]
    assert queue["id"] == queue_id
    assert queue["created_by"] == people[OWNER].user_id


def test_the_owner_updates_the_queue(people, make_queue):
    """The owner renames the queue it created.

    Expect: the update reports one queue updated, and the queue carries the new display name.
    """
    queue_id = make_queue("updated")
    result = people[OWNER].api.authed("queues.update", {"queue": queue_id, "display_name": "Renamed by owner"})
    assert result["updated"] == 1
    queue = people[OWNER].api.authed("queues.get_by_id", {"queue": queue_id})["queue"]
    assert queue["display_name"] == "Renamed by owner"


def test_the_owner_sets_its_queue_public(people, make_queue):
    """The owner makes its queue public.

    Expect: the update reports one queue updated, and the queue reads as public.
    """
    queue_id = make_queue("public")
    result = people[OWNER].api.authed("queues.update", {"queue": queue_id, "public": True})
    assert result["updated"] == 1
    assert people[OWNER].api.authed("queues.get_by_id", {"queue": queue_id})["queue"]["public"] is True


def test_the_owner_adds_and_removes_a_grant(people, make_queue):
    """The owner grants the queue to another user, and later removes the grant.

    Expect: the grant is listed for the queue after it is added, and gone after it is removed.
    """
    queue_id = make_queue("grant")
    owner = people[OWNER].api
    assert owner.authed("queues.add_user", {"queue": queue_id, "user": people[GRANTEE].user_id})["updated"] == 1
    granted = [u["user"] for u in owner.authed("queues.get_users", {"queue": queue_id})["users"]]
    assert granted == [people[GRANTEE].user_id]

    assert owner.authed("queues.remove_user", {"queue": queue_id, "user": people[GRANTEE].user_id})["updated"] == 1
    assert owner.authed("queues.get_users", {"queue": queue_id})["users"] == []


def test_the_owner_sets_the_metadata_of_its_queue(people, make_queue):
    """The owner adds metadata to the queue it created.

    Expect: the metadata is stored on the queue under its key.
    """
    queue_id = make_queue("metadata")
    owner = people[OWNER].api
    owner.authed("queues.add_or_update_metadata", {"queue": queue_id, "metadata": [
        {"key": "team", "type": "str", "value": "ml-platform"}]})
    queue = owner.authed("queues.get_by_id", {"queue": queue_id})["queue"]
    assert "team" in queue["metadata"], queue["metadata"]


def test_a_grantee_enqueues_a_task_in_the_queue(people, make_queue, task_projects):
    """A user who holds a grant on a queue may enqueue a task in it.

    Expect: the enqueue reports the task queued.
    """
    queue_id = make_queue("grantee-enqueue")
    people[OWNER].api.authed("queues.add_user", {"queue": queue_id, "user": people[GRANTEE].user_id})
    task_id = _push(people[GRANTEE], task_projects[GRANTEE], "queue-grantee-enqueue")
    result = people[GRANTEE].api.authed("tasks.enqueue", {"task": task_id, "queue": queue_id})
    assert result["queued"] == 1


def test_a_grantee_cannot_update_or_delete_the_queue(people, make_queue):
    """A grantee uses the queue but does not manage it: it may not update or delete it.

    Expect: the update and the delete are both refused with 403, and the queue is unchanged.
    """
    queue_id = make_queue("grantee-manage")
    people[OWNER].api.authed("queues.add_user", {"queue": queue_id, "user": people[GRANTEE].user_id})
    grantee = people[GRANTEE].api

    _assert_forbidden(_refused(grantee, "queues.update", {"queue": queue_id, "display_name": "by grantee"}))
    _assert_forbidden(_refused(grantee, "queues.delete", {"queue": queue_id}))
    queue = people[OWNER].api.authed("queues.get_by_id", {"queue": queue_id})["queue"]
    assert queue.get("display_name") != "by grantee"


def test_a_grantee_cannot_add_a_grant_or_set_metadata(people, make_queue):
    """A grantee may not give the queue to anyone else, nor change its metadata.

    Expect: adding a grant and adding metadata are both refused with 403.
    """
    queue_id = make_queue("grantee-grants")
    people[OWNER].api.authed("queues.add_user", {"queue": queue_id, "user": people[GRANTEE].user_id})
    grantee = people[GRANTEE].api

    _assert_forbidden(_refused(grantee, "queues.add_user", {"queue": queue_id, "user": people[OUTSIDER].user_id}))
    _assert_forbidden(_refused(grantee, "queues.add_or_update_metadata", {"queue": queue_id, "metadata": [
        {"key": "team", "type": "str", "value": "grantee"}]}))


def test_a_grantee_cannot_set_the_queue_public(people, make_queue):
    """Only the owner sets the public flag of a queue, so a grantee may not make it public.

    Expect: the update is refused with 403, and the queue stays private.
    """
    queue_id = make_queue("grantee-public")
    people[OWNER].api.authed("queues.add_user", {"queue": queue_id, "user": people[GRANTEE].user_id})

    _assert_forbidden(_refused(people[GRANTEE].api, "queues.update", {"queue": queue_id, "public": True}))
    assert people[OWNER].api.authed("queues.get_by_id", {"queue": queue_id})["queue"]["public"] is False


def test_a_project_member_sees_only_the_allocated_projects_they_belong_to(people, make_queue, make_root):
    """A queue is allocated to two root projects of the owner; a member of one of them sees only that one.

    Expect: the member's list of projects for the queue holds the project the member is in, and not the other one.
    """
    queue_id = make_queue("member-projects")
    member_project = make_root("member-project", members=[(MEMBER, "read")])
    other_project = make_root("other-project")
    _allocate(people[OWNER], member_project, queue_id)
    _allocate(people[OWNER], other_project, queue_id)

    projects = people[MEMBER].api.authed("queues.get_projects", {"queue": queue_id})["projects"]
    assert [p["id"] for p in projects] == [member_project]
    assert people[MEMBER].api.authed("queues.get_by_id", {"queue": queue_id})["queue"]["id"] == queue_id


def test_an_outsider_cannot_see_or_use_the_queue(people, make_queue):
    """A user with no grant and no project in common with the queue cannot see it, list it or enqueue into it.

    Expect: reading the queue, listing its projects and enqueueing a task into it are refused with 400 'Invalid queue
    id', and the queue is absent from the outsider's list of queues.
    """
    queue_id = make_queue("outsider")
    outsider = people[OUTSIDER].api

    _assert_no_queue(_refused(outsider, "queues.get_by_id", {"queue": queue_id}))
    _assert_no_queue(_refused(outsider, "queues.get_projects", {"queue": queue_id}))

    project_id = workspace.create_project(outsider, new_name("queue-outsider-tasks"))
    try:
        task_id = _push(people[OUTSIDER], project_id, "queue-outsider-enqueue")
        _assert_no_queue(_refused(outsider, "tasks.enqueue", {"task": task_id, "queue": queue_id}))
    finally:
        workspace.delete_project(outsider, project_id)
    listed = [q["id"] for q in outsider.authed("queues.get_all", {})["queues"]]
    assert queue_id not in listed


def test_an_admin_sees_every_queue_and_every_allocated_project(people, admin, make_queue, make_root):
    """The company admin is not on the queue or its projects, and still sees both.

    Expect: the admin's list of queues holds the queue, and the admin's list of projects for it holds both projects it
    is allocated to, though the admin is a member of neither.
    """
    queue_id = make_queue("admin")
    first = make_root("admin-first")
    second = make_root("admin-second")
    _allocate(people[OWNER], first, queue_id)
    _allocate(people[OWNER], second, queue_id)

    assert queue_id in [q["id"] for q in admin.authed("queues.get_all", {})["queues"]]
    projects = [p["id"] for p in admin.authed("queues.get_projects", {"queue": queue_id})["projects"]]
    assert first in projects and second in projects, projects


def test_a_grantee_dequeue_is_refused_once_the_grant_is_removed(people, make_queue, task_projects):
    """A task a grantee enqueued stays queued after its grant is removed; the grantee may no longer take it out.

    Expect: the grantee's dequeue is refused with 400 'Invalid queue id', and the task is still queued.
    """
    queue_id = make_queue("grant-removed")
    owner = people[OWNER].api
    owner.authed("queues.add_user", {"queue": queue_id, "user": people[GRANTEE].user_id})
    task_id = _push(people[GRANTEE], task_projects[GRANTEE], "queue-grant-removed")
    people[GRANTEE].api.authed("tasks.enqueue", {"task": task_id, "queue": queue_id})
    owner.authed("queues.remove_user", {"queue": queue_id, "user": people[GRANTEE].user_id})

    _assert_no_queue(_refused(people[GRANTEE].api, "tasks.dequeue", {"task": task_id}))
    assert people[GRANTEE].api.authed("tasks.get_by_id", {"task": task_id})["task"]["status"] == "queued"


def test_the_owner_dequeues_its_own_task_while_it_can_use_the_queue(people, make_queue, task_projects):
    """The owner enqueues a task into its own queue and takes it out again.

    Expect: the dequeue reports the task dequeued, and the task is no longer in the queue.
    """
    queue_id = make_queue("owner-dequeue")
    owner = people[OWNER].api
    task_id = _push(people[OWNER], task_projects[OWNER], "queue-owner-dequeue")
    owner.authed("tasks.enqueue", {"task": task_id, "queue": queue_id})

    assert owner.authed("tasks.dequeue", {"task": task_id})["dequeued"] == 1
    entries = owner.authed("queues.get_by_id", {"queue": queue_id})["queue"].get("entries", [])
    assert task_id not in [e["task"] for e in entries]


def test_a_worker_for_a_queue_the_user_neither_owns_nor_holds_is_refused(people, admin, make_queue):
    """A user may not have a worker listen to a private queue of someone else, without owning it or holding a grant.

    Expect: the worker registration is refused with 400 'Invalid queue id', and no worker of that name is registered.
    """
    queue_id = make_queue("worker-refused")
    worker = new_name("queue-worker-refused")

    _assert_no_queue(_refused(people[OUTSIDER].api, "workers.register", {"worker": worker, "queues": [queue_id]}))
    assert worker_record(admin, worker) is None


def test_a_worker_for_a_public_queue_is_accepted(people, admin, make_queue):
    """A public queue may be served by any user's worker, so a registration naming it is accepted.

    Expect: the registration is accepted, the worker is listed with the queue, and it is unregistered afterwards.
    """
    queue_id = make_queue("worker-public")
    owner = people[OWNER].api
    owner.authed("queues.update", {"queue": queue_id, "public": True})
    queue_name = owner.authed("queues.get_by_id", {"queue": queue_id})["queue"]["name"]
    worker = new_name("queue-worker-public")
    outsider = people[OUTSIDER].api
    outsider.authed("workers.register", {"worker": worker, "queues": [queue_id]})
    try:
        record = worker_record(admin, worker)
        assert record is not None
        assert queue_name in [q.get("name") for q in record.get("queues", [])], record
    finally:
        outsider.authed("workers.unregister", {"worker": worker})
