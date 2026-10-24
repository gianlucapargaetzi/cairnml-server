# apiserver/tests/services/test_projects_access_enforcement.py
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.projects import (
    DeleteRequest,
    MergeRequest,
    MoveRequest,
    ProjectRequest,
    ProjectTagsRequest,
)
from apiserver.bll.project.access import ProjectAccessBLL
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity
from apiserver.utilities.partial_version import PartialVersion
from apiserver.services import projects as svc
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    # Every test gets its own company so that projects from other tests never interfere
    return f"c-{uuid4().hex[:8]}"


def make_project(
    company,
    id_,
    owner="owner1",
    parent=None,
    visibility=ProjectVisibility.private,
    members=(),
):
    name = f"{parent.name}/{id_}" if parent else id_
    project = Project(
        id=id_,
        company=company,
        user=owner,
        name=name,
        basename=id_,
        created=datetime.utcnow(),
        visibility=visibility,
        parent=parent.id if parent else None,
        path=[*(parent.path or []), parent.id] if parent else [],
        members=[
            ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner)
            for u, r in members
        ],
    )
    project.save()
    return project


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def test_a_stranger_cannot_see_a_private_project_in_the_visible_ids(company):
    make_project(company, "priv1")
    ids = ProjectAccessBLL.visible_project_ids(identity(company, "stranger"))
    assert "priv1" not in ids


def test_everyone_sees_a_public_project(company):
    make_project(company, "pub1", visibility=ProjectVisibility.public)
    ids = ProjectAccessBLL.visible_project_ids(identity(company, "stranger"))
    assert "pub1" in ids


def test_a_sub_project_follows_its_root_in_the_visible_ids(company):
    public_root = make_project(company, "pubroot", visibility=ProjectVisibility.public)
    private_root = make_project(company, "privroot")
    public_sub = make_project(company, "pubsub", parent=public_root)
    private_sub = make_project(company, "privsub", parent=private_root)

    ids = ProjectAccessBLL.visible_project_ids(identity(company, "stranger"))
    assert public_sub.id in ids
    assert private_sub.id not in ids


def test_a_member_sees_a_private_project_and_its_sub_projects(company):
    root = make_project(company, "memroot", members=[("u2", ProjectRole.read)])
    sub = make_project(company, "memsub", parent=root)
    ids = ProjectAccessBLL.visible_project_ids(identity(company, "u2"))
    assert {root.id, sub.id} <= ids


def test_admin_sees_everything_filter_is_none(company):
    make_project(company, "priv2")
    assert ProjectAccessBLL.visible_project_ids(identity(company, "someone", role=Role.admin)) is None


def test_get_by_id_refuses_a_private_project_to_a_stranger(company):
    make_project(company, "priv3")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.get_by_id(FakeCall(identity(company, "stranger")), company, ProjectRequest(project="priv3"))


def test_get_by_id_returns_a_public_project_to_a_stranger(company):
    make_project(company, "pub2", visibility=ProjectVisibility.public)
    call = FakeCall(identity(company, "stranger"))
    svc.get_by_id(call, company, ProjectRequest(project="pub2"))
    assert call.result.data["project"]["id"] == "pub2"


def test_get_all_lists_only_what_the_caller_can_see(company):
    make_project(company, "listpriv")
    make_project(company, "listpub", visibility=ProjectVisibility.public)
    call = FakeCall(identity(company, "stranger"))
    svc.get_all(call, company, None)
    ids = {p["id"] for p in call.result.data["projects"]}
    assert "listpub" in ids
    assert "listpriv" not in ids


def test_reading_tags_of_a_private_project_is_refused_to_a_stranger(company):
    make_project(company, "tagpriv")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.get_project_tags(
            FakeCall(identity(company, "stranger")),
            company,
            ProjectTagsRequest(projects=["tagpriv"]),
        )


def test_update_refuses_a_read_write_member(company):
    make_project(company, "upd1", members=[("u2", ProjectRole.read_write)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(FakeCall(identity(company, "u2"), data={"name": "New"}), company, ProjectRequest(project="upd1"))


def test_delete_refuses_a_collaborator(company):
    make_project(company, "del1", members=[("u2", ProjectRole.collaborator)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete(
            FakeCall(identity(company, "u2")),
            company,
            DeleteRequest(project="del1", force=True),
        )
    assert Project.objects(id="del1").first() is not None


def test_validate_delete_refuses_a_collaborator(company):
    make_project(company, "del2", members=[("u2", ProjectRole.collaborator)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.validate_delete(FakeCall(identity(company, "u2")), company, ProjectRequest(project="del2"))


def test_move_refuses_a_destination_the_caller_can_only_read(company):
    make_project(company, "src1", members=[("u2", ProjectRole.collaborator)])
    make_project(company, "dst1", visibility=ProjectVisibility.public)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "u2")),
            company,
            MoveRequest(project="src1", new_location="dst1"),
        )
    assert Project.objects(id="src1").first().parent is None


def test_merge_refuses_a_destination_the_caller_cannot_administer(company):
    make_project(company, "msrc", members=[("u2", ProjectRole.collaborator)])
    make_project(company, "mdst", visibility=ProjectVisibility.public)
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.merge(
            FakeCall(identity(company, "u2")),
            company,
            MergeRequest(project="msrc", destination_project="mdst"),
        )
    assert Project.objects(id="msrc").first() is not None


def test_move_into_a_project_resets_visibility_and_members_of_the_moved_root(company):
    make_project(company, "mvsrc", visibility=ProjectVisibility.public, members=[("u2", ProjectRole.read)])
    make_project(company, "mvdst")
    svc.move(
        FakeCall(identity(company, "owner1")),
        company,
        MoveRequest(project="mvsrc", new_location="mvdst"),
    )
    moved = Project.objects(id="mvsrc").first()
    assert moved.parent == "mvdst"
    assert moved.visibility == ProjectVisibility.private
    assert moved.members == []


def test_move_out_of_a_project_resets_the_promoted_sub_project(company):
    root = make_project(company, "mvroot")
    sub = make_project(company, "mvchild", parent=root, visibility=ProjectVisibility.public, members=[("u2", ProjectRole.read)])
    svc.move(
        FakeCall(identity(company, "owner1")),
        company,
        MoveRequest(project=sub.id, new_location=""),
    )
    promoted = Project.objects(id=sub.id).first()
    assert promoted.parent is None
    assert promoted.visibility == ProjectVisibility.private
    assert promoted.members == []


def test_move_between_sub_locations_keeps_the_moved_project_settings(company):
    root_a = make_project(company, "mvra")
    root_b = make_project(company, "mvrb")
    sub = make_project(company, "mvsub", parent=root_a, visibility=ProjectVisibility.public)
    make_project(company, "mvloc", parent=root_b)
    svc.move(
        FakeCall(identity(company, "owner1")),
        company,
        MoveRequest(project=sub.id, new_location="mvrb/mvloc"),
    )
    moved = Project.objects(id=sub.id).first()
    assert moved.parent is not None
    assert moved.visibility == ProjectVisibility.public


def test_reset_access_clears_visibility_and_members(company):
    root = make_project(company, "rsroot", visibility=ProjectVisibility.public, members=[("u2", ProjectRole.collaborator)])
    ProjectAccessBLL.reset_access(root.id)
    refreshed = Project.objects(id="rsroot").first()
    assert refreshed.visibility == ProjectVisibility.private
    assert refreshed.members == []
