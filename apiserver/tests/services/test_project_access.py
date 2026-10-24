# apiserver/tests/services/test_project_access.py
from datetime import datetime
import pytest
from apiserver.apierrors import errors
from apiserver.apimodels.project_access import (
    AddMemberRequest, GetMembersRequest, RemoveMemberRequest,
    SetVisibilityRequest, TransferOwnershipRequest, UpdateMemberRoleRequest,
)
from apiserver.database.model.auth import Role, User
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity
from apiserver.services import project_access as svc
from apiserver.tests.db import ensure_db
ensure_db()

COMPANY = "c1"


def make_root(visibility=ProjectVisibility.private, members=()):
    project = Project(
        id="root1", company=COMPANY, user="owner1", name="Root", basename="Root",
        created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by="owner1") for u, r in members],
    )
    project.save()
    return project


def make_user(user_id):
    User(id=user_id, name=user_id, company=COMPANY, created=datetime.utcnow()).save()


def identity(user, role=Role.user):
    return Identity(user=user, company=COMPANY, role=role)


class FakeCall:
    def __init__(self, ident):
        self.identity = ident


def test_owner_can_add_a_member():
    make_root()
    make_user("u2")
    svc.add_member(FakeCall(identity("owner1")), COMPANY, AddMemberRequest(project="root1", user="u2", role=ProjectRole.read))
    assert Project.objects(id="root1").first().members[0].user == "u2"


def test_read_write_cannot_add_a_member():
    make_root(members=[("u2", ProjectRole.read_write)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.add_member(FakeCall(identity("u2")), COMPANY, AddMemberRequest(project="root1", user="u3", role=ProjectRole.read))


def test_owner_can_change_a_members_role():
    make_root(members=[("u2", ProjectRole.read)])
    svc.update_member_role(FakeCall(identity("owner1")), COMPANY, UpdateMemberRoleRequest(project="root1", user="u2", role=ProjectRole.collaborator))
    assert Project.objects(id="root1").first().members[0].role == ProjectRole.collaborator


def test_owner_can_remove_a_member():
    make_root(members=[("u2", ProjectRole.read)])
    svc.remove_member(FakeCall(identity("owner1")), COMPANY, RemoveMemberRequest(project="root1", user="u2"))
    assert Project.objects(id="root1").first().members == []


def test_owner_can_set_visibility():
    make_root(visibility=ProjectVisibility.private)
    svc.set_visibility(FakeCall(identity("owner1")), COMPANY, SetVisibilityRequest(project="root1", visibility=ProjectVisibility.public))
    assert Project.objects(id="root1").first().visibility == ProjectVisibility.public


def test_transfer_requires_the_target_to_already_be_a_member():
    make_root()
    with pytest.raises(errors.bad_request.InvalidUserId):
        svc.transfer_ownership(FakeCall(identity("owner1")), COMPANY, TransferOwnershipRequest(project="root1", new_owner="stranger"))


def test_the_owner_is_not_a_member_row_so_cannot_be_targeted_by_member_endpoints():
    # Structural guarantee, not just a policy check: the Owner was never appended to `members`
    # (see Task 1/4's data model), so there is no member row for update_member_role/remove_member
    # to find when given the Owner's own user id — this is exactly the anti-goal "a Collaborator
    # manages to strip the Owner's role."
    make_root(members=[("u2", ProjectRole.collaborator)])
    with pytest.raises(errors.bad_request.InvalidUserId):
        svc.update_member_role(FakeCall(identity("u2")), COMPANY, UpdateMemberRoleRequest(project="root1", user="owner1", role=ProjectRole.read))
    with pytest.raises(errors.bad_request.InvalidUserId):
        svc.remove_member(FakeCall(identity("owner1")), COMPANY, RemoveMemberRequest(project="root1", user="owner1"))


def test_transfer_makes_the_target_owner_and_keeps_the_old_owner_as_a_collaborator():
    make_root(members=[("u2", ProjectRole.read)])
    svc.transfer_ownership(FakeCall(identity("owner1")), COMPANY, TransferOwnershipRequest(project="root1", new_owner="u2"))
    project = Project.objects(id="root1").first()
    assert project.user == "u2"
    members_by_user = {m.user: m.role for m in project.members}
    assert "u2" not in members_by_user
    assert members_by_user["owner1"] == ProjectRole.collaborator


def test_only_owner_or_admin_can_transfer():
    make_root(members=[("u2", ProjectRole.collaborator), ("u3", ProjectRole.read)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.transfer_ownership(FakeCall(identity("u2")), COMPANY, TransferOwnershipRequest(project="root1", new_owner="u3"))
