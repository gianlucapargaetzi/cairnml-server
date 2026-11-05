"""The authorisation table: which level of a resource satisfies which action, and who holds which level.

The table below is written out here, row by row, from the access-model design. The access module must agree with it.
"""

from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import access
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.queue import Queue
from apiserver.database.model.share import Share
from apiserver.database.model.user_access import UserAccess
from apiserver.tests.db import ensure_db

ensure_db()

# (resource type, level, action) -> allowed
ACTION_TABLE = [
    ("queue", "read", "use", True),
    ("queue", "read", "change_content", False),
    ("queue", "read", "manage_sharing", False),
    ("queue", "read_write", "use", True),
    ("queue", "read_write", "change_content", True),
    ("queue", "read_write", "manage_sharing", False),
    ("queue", "collaborator", "use", True),
    ("queue", "collaborator", "change_content", True),
    ("queue", "collaborator", "manage_sharing", True),
    ("dataset", "read", "use", True),
    ("dataset", "read", "change_content", False),
    ("dataset", "read", "manage_sharing", False),
    ("dataset", "read_write", "use", True),
    ("dataset", "read_write", "change_content", True),
    ("dataset", "read_write", "manage_sharing", False),
    ("dataset", "collaborator", "use", True),
    ("dataset", "collaborator", "change_content", True),
    ("dataset", "collaborator", "manage_sharing", True),
    ("registry", "read", "use", True),
    ("registry", "read", "change_content", False),
    ("registry", "read", "manage_sharing", False),
    ("registry", "read_write", "use", True),
    ("registry", "read_write", "change_content", False),
    ("registry", "read_write", "manage_sharing", False),
    ("registry", "collaborator", "use", True),
    ("registry", "collaborator", "change_content", False),
    ("registry", "collaborator", "manage_sharing", True),
    ("credential", "read", "use", True),
    ("credential", "read", "change_content", False),
    ("credential", "read", "manage_sharing", False),
    ("credential", "read_write", "use", True),
    ("credential", "read_write", "change_content", False),
    ("credential", "read_write", "manage_sharing", False),
    ("credential", "collaborator", "use", True),
    ("credential", "collaborator", "change_content", False),
    ("credential", "collaborator", "manage_sharing", True),
]


@pytest.mark.parametrize("resource_type, level, action, expected", ACTION_TABLE)
def test_action_table_row(resource_type, level, action, expected):
    assert access.allows(level, action, resource_type) is expected


@pytest.mark.parametrize("resource_type", ["queue", "dataset", "registry", "credential"])
@pytest.mark.parametrize("action", ["use", "change_content", "manage_sharing"])
def test_no_level_allows_nothing(resource_type, action):
    assert access.allows(None, action, resource_type) is False


def test_an_unknown_action_or_type_is_refused_at_every_level():
    for level in ("read", "read_write", "collaborator"):
        assert access.allows(level, "delete", "queue") is False
        assert access.allows(level, "use", "unknown") is False


# --- level_of, against the database


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def now():
    return datetime.utcnow()


def make_root(company, id_="root1", owner="owner1", members=()):
    project = Project(
        id=id_, company=company, user=owner, name=id_, basename=id_, created=now(),
        members=[ProjectMember(user=u, role=r, added=now(), added_by=owner) for u, r in members],
    )
    project.save()
    return project


def make_queue(company, id_="q1", owner="owner1"):
    Queue(id=id_, company=company, name=f"queue-{id_}", created=now(), created_by=owner).save()
    return id_


def make_dataset(company, id_="d1", owner="owner1", kind="repo"):
    Dataset(id=id_, company=company, kind=kind, ref="o/r", name=id_, owner=owner).save()
    return id_


def make_credential(company, id_="a1", owner="owner1"):
    UserAccess(
        id=id_, company=company, user=owner, kind="token", label=id_, host="github.com", path="", secret="x",
        created=now(), updated=now(),
    ).save()
    return id_


def make_user(id_, company, role=Role.user, disabled=False):
    User(id=id_, company=company, name=id_, role=role, created=now(), idp_disabled=disabled).save()


def make_share(company, resource_type, resource_id, scope, level, project="", user="", granted_by="owner1", id_=None):
    share = Share(
        id=id_ or uuid4().hex, company=company, resource_type=resource_type, resource_id=resource_id, scope=scope,
        project=project, user=user, level=level, granted_by=granted_by, created=now(),
    )
    share.save()
    return share


def resource(company, resource_type):
    """ a resource of the type, owned by owner1; returns its id """
    if resource_type == "queue":
        return make_queue(company)
    if resource_type == "dataset":
        return make_dataset(company)
    if resource_type == "registry":
        return make_dataset(company, kind="registry")
    return make_credential(company)


TYPES = ["queue", "dataset", "registry", "credential"]


@pytest.mark.parametrize("resource_type", TYPES)
def test_the_owner_is_a_collaborator(company, resource_type):
    rid = resource(company, resource_type)
    assert access.level_of(company, "owner1", False, resource_type, rid, set()) == "collaborator"


@pytest.mark.parametrize("resource_type", TYPES)
def test_an_admin_is_a_collaborator_of_every_resource(company, resource_type):
    rid = resource(company, resource_type)
    assert access.level_of(company, "stranger", True, resource_type, rid, set()) == "collaborator"


@pytest.mark.parametrize("resource_type", TYPES)
def test_a_stranger_with_no_share_has_no_level(company, resource_type):
    rid = resource(company, resource_type)
    assert access.level_of(company, "stranger", False, resource_type, rid, set()) is None


@pytest.mark.parametrize("resource_type", TYPES)
def test_a_public_share_gives_read_to_everyone(company, resource_type):
    rid = resource(company, resource_type)
    make_share(company, resource_type, rid, "public", "read")
    assert access.level_of(company, "stranger", False, resource_type, rid, set()) == "read"
    assert access.allows(access.level_of(company, "stranger", False, resource_type, rid, set()), "use", resource_type)


def test_a_public_share_is_read_only_for_a_queue_too(company):
    make_queue(company)
    make_share(company, "queue", "q1", "public", "read")
    level = access.level_of(company, "stranger", False, "queue", "q1", set())
    assert level == "read"
    assert access.allows(level, "change_content", "queue") is False


@pytest.mark.parametrize("resource_type, level", [
    ("queue", "read"), ("queue", "read_write"), ("queue", "collaborator"),
    ("dataset", "read"), ("dataset", "read_write"), ("dataset", "collaborator"),
    ("registry", "read"), ("registry", "collaborator"),
    ("credential", "read"), ("credential", "collaborator"),
])
def test_an_active_project_member_holds_the_shared_level(company, resource_type, level):
    rid = resource(company, resource_type)
    make_root(company, members=[("member1", ProjectRole.read)])
    make_share(company, resource_type, rid, "projects", level, project="root1")
    assert access.level_of(company, "member1", False, resource_type, rid, {"root1"}) == level


def test_a_project_member_outside_project_roots_has_no_level(company):
    make_queue(company)
    make_root(company, members=[("member1", ProjectRole.read)])
    make_share(company, "queue", "q1", "projects", "read_write", project="root1")
    assert access.level_of(company, "member1", False, "queue", "q1", set()) is None


def test_a_removed_project_member_loses_the_level(company):
    make_queue(company)
    root = make_root(company, members=[("member1", ProjectRole.read)])
    make_share(company, "queue", "q1", "projects", "read_write", project="root1")
    assert access.level_of(company, "member1", False, "queue", "q1", {"root1"}) == "read_write"

    root.members = []
    root.save()
    assert access.level_of(company, "member1", False, "queue", "q1", {"root1"}) is None


def test_a_grant_stops_when_the_granting_owner_leaves_the_project(company):
    make_queue(company)
    root = make_root(company, members=[("member1", ProjectRole.read)])
    make_share(company, "queue", "q1", "projects", "read", project="root1", granted_by="owner2")
    root.members = [ProjectMember(user="member1", role=ProjectRole.read, added=now(), added_by="owner1")]
    root.save()
    assert access.level_of(company, "member1", False, "queue", "q1", {"root1"}) is None


def test_a_grant_stops_when_the_granting_owner_is_deactivated(company):
    make_queue(company)
    make_root(company, members=[("member1", ProjectRole.read), ("owner2", ProjectRole.read)])
    make_user("owner2", company, disabled=True)
    make_share(company, "queue", "q1", "projects", "read", project="root1", granted_by="owner2")
    assert access.level_of(company, "member1", False, "queue", "q1", {"root1"}) is None


def test_the_highest_applicable_share_wins(company):
    make_queue(company)
    make_root(company, members=[("member1", ProjectRole.read)])
    make_share(company, "queue", "q1", "public", "read")
    make_share(company, "queue", "q1", "projects", "read_write", project="root1")
    assert access.level_of(company, "member1", False, "queue", "q1", {"root1"}) == "read_write"


def test_a_user_share_gives_read_to_that_user_only(company):
    make_queue(company)
    make_share(company, "queue", "q1", "user", "read", user="grantee")
    assert access.level_of(company, "grantee", False, "queue", "q1", set()) == "read"
    assert access.level_of(company, "other", False, "queue", "q1", set()) is None


def test_a_user_share_on_a_queue_does_not_allow_changing_it(company):
    make_queue(company)
    make_share(company, "queue", "q1", "user", "read", user="grantee")
    level = access.level_of(company, "grantee", False, "queue", "q1", set())
    assert access.allows(level, "use", "queue") is True
    assert access.allows(level, "change_content", "queue") is False


def test_a_share_on_one_resource_does_not_reach_another(company):
    make_queue(company, id_="q1")
    make_queue(company, id_="q2")
    make_share(company, "queue", "q1", "public", "read")
    assert access.level_of(company, "stranger", False, "queue", "q2", set()) is None


# --- require: a missing level is not-found, a level without the action is forbidden


def test_require_on_a_queue_with_no_level_is_not_found(company):
    make_queue(company)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        access.require(company, "stranger", False, "queue", "q1", set(), "use")


def test_require_on_a_dataset_with_no_level_is_not_found(company):
    make_dataset(company)
    with pytest.raises(errors.bad_request.ValidationError):
        access.require(company, "stranger", False, "dataset", "d1", set(), "use")


def test_require_with_a_level_missing_the_action_is_forbidden(company):
    make_queue(company)
    make_share(company, "queue", "q1", "public", "read")
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.require(company, "stranger", False, "queue", "q1", set(), "change_content")


def test_require_with_the_action_allowed_passes(company):
    make_queue(company)
    make_share(company, "queue", "q1", "public", "read")
    access.require(company, "stranger", False, "queue", "q1", set(), "use")


def test_require_on_a_registry_change_content_is_forbidden_even_for_the_owner_level(company):
    make_dataset(company, kind="registry")
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.require(company, "owner1", False, "registry", "d1", set(), "change_content")


# --- share and unshare


def test_the_owner_shares_a_queue_with_a_project(company):
    make_queue(company)
    make_root(company, id_="root1", owner="owner1")
    share = access.share(company, "owner1", "queue", "q1", "projects", "root1", "read_write")
    assert share.granted_by == "owner1"
    assert Share.objects(company=company, resource_type="queue", resource_id="q1", project="root1").count() == 1


def test_a_reader_may_not_share(company):
    make_queue(company)
    make_share(company, "queue", "q1", "public", "read")
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.share(company, "stranger", "queue", "q1", "public", "", "read")


def test_a_user_with_no_level_cannot_share_a_queue_at_all(company):
    make_queue(company)
    with pytest.raises(errors.bad_request.InvalidQueueId):
        access.share(company, "stranger", "queue", "q1", "public", "", "read")


def test_a_collaborator_may_share(company):
    make_queue(company)
    make_share(company, "queue", "q1", "projects", "collaborator", project="root1", granted_by="owner1")
    make_root(company, id_="root1", members=[("collab", ProjectRole.read)])
    share = access.share(company, "collab", "queue", "q1", "public", "", "read")
    assert share.scope == "public"


def test_a_registry_may_not_be_shared_at_read_write(company):
    make_dataset(company, kind="registry")
    make_root(company, id_="root1", owner="owner1")
    with pytest.raises(errors.bad_request.ValidationError):
        access.share(company, "owner1", "registry", "d1", "projects", "root1", "read_write")


def test_an_admin_may_share_a_resource_they_do_not_own(company):
    make_queue(company)
    make_user("admin1", company, role=Role.admin)
    share = access.share(company, "admin1", "queue", "q1", "public", "", "read")
    assert share.granted_by == "admin1"


def test_a_user_share_is_created_for_the_named_user(company):
    make_queue(company)
    share = access.share(company, "owner1", "queue", "q1", "user", "", "read", user="grantee")
    assert share.user == "grantee"
    assert access.level_of(company, "grantee", False, "queue", "q1", set()) == "read"


def test_a_project_share_to_an_unknown_project_is_refused(company):
    make_queue(company)
    with pytest.raises(errors.bad_request.ValidationError):
        access.share(company, "owner1", "queue", "q1", "projects", "no-such-root", "read")


def test_the_same_share_twice_is_refused(company):
    make_queue(company)
    access.share(company, "owner1", "queue", "q1", "public", "", "read")
    with pytest.raises(errors.bad_request.ValidationError):
        access.share(company, "owner1", "queue", "q1", "public", "", "read")


def test_a_reader_may_not_unshare(company):
    make_queue(company)
    share = make_share(company, "queue", "q1", "public", "read")
    make_share(company, "queue", "q1", "user", "read", user="reader")
    with pytest.raises(errors.forbidden.NoWritePermission):
        access.unshare(company, "reader", share.id)


def test_the_owner_unshares_and_the_level_is_gone(company):
    make_queue(company)
    share = make_share(company, "queue", "q1", "public", "read")
    access.unshare(company, "owner1", share.id)
    assert access.level_of(company, "stranger", False, "queue", "q1", set()) is None
