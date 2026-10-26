"""Per-project access control, end to end: membership, roles and visibility, through the REST API as enrolled devices.

The projects belong to a plain user (the owner), so the admin is an admin and not an owner in the scenarios that
say so. Each role is a plain user of its own, enrolled through the sign-in page. A private project is invisible to
anyone not on it: a request for its id gets the same answer a genuinely missing id gets.
"""

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from support import client, settings, workspace
from support.api import Api, ApiError
from support.workspace import new_name

# The membership and visibility endpoints exist from this API version (services/projects.conf, 2.37).
MEMBERSHIP_VERSION = "2.37"
TASK_TYPE = "training"

OWNER, READ_WRITE, COLLABORATOR, READER, OUTSIDER = "owner", "read_write", "collaborator", "read", "outsider"
MEMBER_ROLES = {READ_WRITE: "read_write", COLLABORATOR: "collaborator", READER: "read"}


class VersionedApi(Api):
    """The same device as the given Api, calling the API at another version."""

    def __init__(self, api: Api, version: str):
        self.device_id = api.device_id
        self.key = api.key
        self.http = api.http
        self.version = version

    @property
    def base(self) -> str:
        return f"{settings.API_URL}/v{self.version}/"


@dataclass
class Person:
    label: str
    home: Path
    api: Api
    user_id: str


def _refused(api: Api, endpoint: str, body: dict) -> ApiError:
    with pytest.raises(ApiError) as refused:
        api.authed(endpoint, body)
    return refused.value


def _assert_forbidden(error: ApiError):
    assert error.status == 403, f"expected a 403 refusal, got {error}"


def _assert_hidden(error: ApiError, project_id: str):
    """A private project the caller cannot see: the same answer as a missing id (400, 'Invalid project id')."""
    assert error.status == 400, f"expected the missing-project answer, got {error}"
    assert error.message == f"Invalid project id: id={project_id}", f"expected the missing-project answer, got {error}"


def _push(person: Person, project_id: str, label: str) -> str:
    return person.api.authed("tasks.create", {"name": new_name(label), "type": TASK_TYPE,
                                              "project": project_id})["id"]


@pytest.fixture(scope="module")
def people(keycloak, tmp_path_factory):
    """One plain user per role, each enrolled on a machine of its own, removed afterwards."""
    accounts = {}
    enrolled = {}
    try:
        for label in (OWNER, READ_WRITE, COLLABORATOR, READER, OUTSIDER):
            username = new_name(f"access-{label}")
            password = os.urandom(12).hex()
            keycloak.create_user(username, password)
            accounts[label] = username
            home = tmp_path_factory.mktemp(f"access-{label}")
            result = client.init(home, (username, password), name=f"e2e-{label}")
            assert result.returncode == 0, result.output
            api = Api(client.device_id_of(home), client.key_path(home))
            user_id = api.authed("users.get_current_user")["user"]["id"]
            enrolled[label] = Person(label, home, api, user_id)
        yield enrolled
    finally:
        for person in enrolled.values():
            client.logout(person.home)
        for username in accounts.values():
            keycloak.delete_user(username)


@pytest.fixture(scope="module")
def private_project(people):
    """A private project owned by OWNER: READ_WRITE, COLLABORATOR and READER are members, OUTSIDER is not."""
    owner = people[OWNER]
    project_id = workspace.create_project(owner.api, new_name("access-private"))
    members = VersionedApi(owner.api, MEMBERSHIP_VERSION)
    for label, role in MEMBER_ROLES.items():
        members.authed("projects.add_member", {"project": project_id, "user": people[label].user_id, "role": role})
    yield project_id
    workspace.delete_project(owner.api, project_id)


@pytest.fixture(scope="module")
def public_project(people):
    """A public project owned by OWNER, with one task in it."""
    owner = people[OWNER]
    project_id = workspace.create_project(owner.api, new_name("access-public"))
    VersionedApi(owner.api, MEMBERSHIP_VERSION).authed("projects.set_visibility", {"project": project_id,
                                                                                    "visibility": "public"})
    yield project_id
    workspace.delete_project(owner.api, project_id)


def test_read_write_member_pushes_a_task_into_the_project(people, private_project):
    """A Read/Write member may push a task into the project.

    Expect: the push returns the id of a new task.
    """
    task_id = _push(people[READ_WRITE], private_project, "access-rw-push")
    assert task_id


def test_read_write_member_deletes_its_own_task(people, private_project):
    """A Read/Write member may delete a task it pushed itself.

    Expect: the delete reports the task deleted.
    """
    task_id = _push(people[READ_WRITE], private_project, "access-rw-own")
    result = people[READ_WRITE].api.authed("tasks.delete", {"task": task_id, "force": True})
    assert result["deleted"] is True


def test_read_write_member_cannot_delete_another_members_task(people, private_project):
    """A Read/Write member may not delete a task another member pushed.

    Expect: the delete is refused with 403.
    """
    task_id = _push(people[OWNER], private_project, "access-owner-task")
    error = _refused(people[READ_WRITE].api, "tasks.delete", {"task": task_id, "force": True})
    _assert_forbidden(error)


def test_collaborator_cannot_delete_another_members_task(people, private_project):
    """A Collaborator may delete only the tasks it pushed itself ('own only', the same rule a Read/Write member has).

    Expect: the delete of another member's task is refused with 403.
    """
    task_id = _push(people[READ_WRITE], private_project, "access-rw-for-collaborator")
    error = _refused(people[COLLABORATOR].api, "tasks.delete", {"task": task_id, "force": True})
    _assert_forbidden(error)


def test_collaborator_edits_another_members_task(people, private_project):
    """A Collaborator may edit any task in the project, including another member's (edit is allowed on anyone's
    task, delete only on its own).

    Expect: the rename is applied, and the task is updated once.
    """
    task_id = _push(people[READ_WRITE], private_project, "access-rw-for-collaborator-edit")
    result = people[COLLABORATOR].api.authed("tasks.update", {"task": task_id, "name": new_name("renamed")})
    assert result["updated"] == 1


def test_collaborator_cannot_delete_the_project(people, private_project):
    """Only the Owner may delete a project; a Collaborator may not.

    Expect: the Collaborator's delete is refused with 403, and the project still exists.
    """
    error = _refused(people[COLLABORATOR].api, "projects.delete", {"project": private_project, "force": True})
    _assert_forbidden(error)
    project = people[OWNER].api.authed("projects.get_by_id", {"project": private_project})["project"]
    assert project["id"] == private_project


def test_read_member_cannot_push_a_task(people, private_project):
    """A plain Read member may read the project but not push a task into it.

    Expect: the push is refused with 403.
    """
    error = _refused(people[READER].api, "tasks.create", {"name": new_name("access-reader-push"),
                                                           "type": TASK_TYPE, "project": private_project})
    _assert_forbidden(error)


def test_outsider_cannot_tell_a_private_project_from_a_missing_one_on_read(people, private_project):
    """A non-member of a private project gets the answer a genuinely missing id gets: the project looks absent.

    Expect: reading the private project and reading a missing id are both refused with 400 'Invalid project id'.
    """
    outsider = people[OUTSIDER].api
    missing_id = uuid.uuid4().hex
    hidden = _refused(outsider, "projects.get_by_id", {"project": private_project})
    missing = _refused(outsider, "projects.get_by_id", {"project": missing_id})

    _assert_hidden(hidden, private_project)
    _assert_hidden(missing, missing_id)


def test_outsider_cannot_tell_a_private_project_from_a_missing_one_on_push(people, private_project):
    """A non-member of a private project gets the answer a genuinely missing id gets: the project looks absent.

    Expect: pushing into the private project and into a missing id are refused with the same 400 'Invalid project id'.
    """
    outsider = people[OUTSIDER].api
    push = {"name": new_name("access-outsider-push"), "type": TASK_TYPE}
    missing_id = uuid.uuid4().hex
    hidden = _refused(outsider, "tasks.create", {**push, "project": private_project})
    missing = _refused(outsider, "tasks.create", {**push, "project": missing_id})

    _assert_hidden(hidden, private_project)
    _assert_hidden(missing, missing_id)


def test_outsider_reads_a_public_projects_tasks(people, public_project):
    """A public project is readable by every signed-in user, members or not.

    Expect: a non-member sees the project and the task in it.
    """
    task_id = _push(people[OWNER], public_project, "access-public-task")
    outsider = people[OUTSIDER].api
    assert outsider.authed("projects.get_by_id", {"project": public_project})["project"]["id"] == public_project
    tasks = outsider.authed("tasks.get_all", {"project": [public_project]})["tasks"]
    assert task_id in [task["id"] for task in tasks]


def test_outsider_cannot_enqueue_a_task_in_a_public_project(people, public_project):
    """A public project gives a non-member Read only, so the non-member may not enqueue a task in it.

    Expect: the enqueue is refused with 403.
    """
    task_id = _push(people[OWNER], public_project, "access-public-enqueue")
    error = _refused(people[OUTSIDER].api, "tasks.enqueue", {"task": task_id})
    _assert_forbidden(error)


def test_admin_pushes_and_deletes_in_a_private_project_it_is_not_on(client_home, private_project):
    """The company admin is not a member of the project and needs no membership to act in it.

    Expect: the admin pushes a task into the private project and deletes it, with the project's members in place.
    """
    admin = workspace.device_api(client_home)
    task_id = admin.authed("tasks.create", {"name": new_name("access-admin-push"), "type": TASK_TYPE,
                                            "project": private_project})["id"]
    assert admin.authed("tasks.delete", {"task": task_id, "force": True})["deleted"] is True


def test_admin_deletes_a_members_task_in_a_private_project(client_home, people, private_project):
    """The admin may delete a task a member pushed, though it is not the admin's own.

    Expect: the delete reports the task deleted.
    """
    task_id = _push(people[READ_WRITE], private_project, "access-rw-for-admin")
    admin = workspace.device_api(client_home)
    assert admin.authed("tasks.delete", {"task": task_id, "force": True})["deleted"] is True


def test_admin_enqueues_a_task_in_a_private_project_it_is_not_on(client_home, people, private_project):
    """The admin may enqueue a task in a private project it is not a member of. The queue is one no agent listens to,
    so the task stays queued.

    Expect: the enqueue reports the task queued.
    """
    task_id = _push(people[READ_WRITE], private_project, "access-admin-enqueue")
    admin = workspace.device_api(client_home)
    result = admin.authed("tasks.enqueue", {"task": task_id, "queue_name": new_name("access-queue")})
    assert result["queued"] == 1
