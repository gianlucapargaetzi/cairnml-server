# apiserver/tests/bll/project/test_access.py
from datetime import datetime
import pytest
from apiserver.apierrors import errors
from apiserver.bll.project.access import ProjectAccessBLL
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectMember, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity
from apiserver.tests.db import ensure_db
ensure_db()

COMPANY = "c1"


def make_root(visibility=ProjectVisibility.private, members=()):
    project = Project(
        id="root1", company=COMPANY, user="owner1", name="Root",
        basename="Root", created=datetime.utcnow(), visibility=visibility,
        members=[ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by="owner1") for u, r in members],
    )
    project.save()
    return project


def make_sub(root):
    sub = Project(
        id="sub1", company=COMPANY, user="someone", name="Root/Sub", basename="Sub",
        created=datetime.utcnow(), parent=root.id, path=[root.id],
    )
    sub.save()
    return sub


def identity(user, role=Role.user):
    return Identity(user=user, company=COMPANY, role=role)


class TestGetRole:
    def test_admin_is_admin_with_no_membership_row(self):
        make_root()
        assert ProjectAccessBLL.get_role(identity("stranger", role=Role.admin), "root1") == "admin"

    def test_owner_is_owner(self):
        make_root()
        assert ProjectAccessBLL.get_role(identity("owner1"), "root1") == "owner"

    def test_a_listed_member_gets_their_stored_role(self):
        make_root(members=[("u2", ProjectRole.collaborator)])
        assert ProjectAccessBLL.get_role(identity("u2"), "root1") == ProjectRole.collaborator

    def test_a_non_member_on_a_public_project_gets_implicit_read(self):
        make_root(visibility=ProjectVisibility.public)
        assert ProjectAccessBLL.get_role(identity("stranger"), "root1") == ProjectRole.read

    def test_a_non_member_on_a_private_project_gets_no_role(self):
        make_root(visibility=ProjectVisibility.private)
        assert ProjectAccessBLL.get_role(identity("stranger"), "root1") is None

    def test_a_sub_project_resolves_the_roots_role(self):
        root = make_root(members=[("u2", ProjectRole.read)])
        make_sub(root)
        assert ProjectAccessBLL.get_role(identity("u2"), "sub1") == ProjectRole.read

    def test_a_missing_project_id_gets_no_role(self):
        assert ProjectAccessBLL.get_role(identity("anyone"), "does-not-exist") is None

    def test_a_project_in_another_company_gets_no_role(self):
        make_root()
        other_company_identity = Identity(user="owner1", company="other-company", role=Role.user)
        assert ProjectAccessBLL.get_role(other_company_identity, "root1") is None


class TestAssertCan:
    def test_no_role_raises_invalid_project_id_not_forbidden(self):
        make_root(visibility=ProjectVisibility.private)
        with pytest.raises(errors.bad_request.InvalidProjectId):
            ProjectAccessBLL.assert_can(identity("stranger"), "root1", "read")

    def test_read_write_can_write_their_own_object(self):
        make_root(members=[("u2", ProjectRole.read_write)])
        ProjectAccessBLL.assert_can(identity("u2"), "root1", "write", object_owner="u2")

    def test_read_write_cannot_write_someone_elses_object(self):
        make_root(members=[("u2", ProjectRole.read_write)])
        with pytest.raises(errors.forbidden.NoWritePermission):
            ProjectAccessBLL.assert_can(identity("u2"), "root1", "write", object_owner="someone-else")

    def test_collaborator_can_write_someone_elses_object_but_not_delete_it(self):
        make_root(members=[("u2", ProjectRole.collaborator)])
        ProjectAccessBLL.assert_can(identity("u2"), "root1", "write", object_owner="someone-else")
        with pytest.raises(errors.forbidden.NoWritePermission):
            ProjectAccessBLL.assert_can(identity("u2"), "root1", "delete", object_owner="someone-else")

    def test_collaborator_can_manage_members_owner_only_actions_are_refused(self):
        make_root(members=[("u2", ProjectRole.collaborator)])
        ProjectAccessBLL.assert_can(identity("u2"), "root1", "admin")
        with pytest.raises(errors.forbidden.NoWritePermission):
            ProjectAccessBLL.assert_can(identity("u2"), "root1", "owner_only")

    def test_owner_passes_every_capability(self):
        make_root()
        for capability in ("read", "write", "delete", "admin", "owner_only"):
            ProjectAccessBLL.assert_can(identity("owner1"), "root1", capability, object_owner="anyone")

    def test_admin_passes_every_capability_on_a_private_project_with_no_membership(self):
        make_root(visibility=ProjectVisibility.private)
        for capability in ("read", "write", "delete", "admin", "owner_only"):
            ProjectAccessBLL.assert_can(identity("someone", role=Role.admin), "root1", capability, object_owner="anyone")
