from typing import Optional, Set

from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectRole, ProjectVisibility
from apiserver.service_repo.auth import Identity

_ADMIN_ROLES = {ProjectRole.collaborator}
_CAPABILITIES = ("read", "write", "delete", "admin", "owner_only")


class ProjectAccessBLL:
    @staticmethod
    def resolve_root_id(project_id: str, company: str) -> Optional[str]:
        project = Project.objects(id=project_id, company=company).only("id", "path").first()
        if not project:
            return None
        return project.path[0] if project.path else project.id

    @staticmethod
    def get_role(identity: Identity, project_id: str) -> Optional[str]:
        root_id = ProjectAccessBLL.resolve_root_id(project_id, identity.company)
        if not root_id:
            return None

        if identity.role == Role.admin:
            return "admin"

        root = Project.objects(id=root_id, company=identity.company).only(
            "user", "visibility", "members"
        ).first()
        if not root:
            return None

        if root.user == identity.user:
            return "owner"

        for member in root.members:
            if member.user == identity.user:
                return member.role

        if root.visibility == ProjectVisibility.public:
            return ProjectRole.read

        return None

    @staticmethod
    def visible_projects_query(identity: Identity) -> Q:
        """
        Root projects the caller may see: their own, public ones, ones they are a member of,
        and public (company-less) projects, which every company may see
        """
        if identity.role == Role.admin:
            return Q()
        return (
            Q(company=identity.company)
            & (
                Q(user=identity.user)
                | Q(visibility=ProjectVisibility.public)
                | Q(members__user=identity.user)
            )
        ) | Q(company="")

    @staticmethod
    def visible_project_ids(identity: Identity) -> Optional[Set[str]]:
        """
        Ids of all projects the caller may see: the visible roots and all their sub-projects.
        Returns None for admins, meaning no filter is needed
        """
        if identity.role == Role.admin:
            return None

        root_ids = [
            p.id
            for p in Project.objects(
                Q(parent=None) & ProjectAccessBLL.visible_projects_query(identity)
            ).only("id")
        ]
        visible = set(root_ids)
        if root_ids:
            visible.update(Project.objects(path__in=root_ids).scalar("id"))
        return visible

    @staticmethod
    def reset_access(project_id: str) -> None:
        """
        Put a project back to the default private visibility without members.
        Used when a move changes whether the project is a root, since visibility and members
        only apply to roots
        """
        Project.objects(id=project_id).update_one(
            set__visibility=ProjectVisibility.private, set__members=[]
        )

    @staticmethod
    def assert_can(
        identity: Identity,
        project_id: str,
        capability: str,
        object_owner: Optional[str] = None,
    ) -> None:
        if capability not in _CAPABILITIES:
            raise ValueError(f"unknown capability: {capability}")

        role = ProjectAccessBLL.get_role(identity, project_id)
        if role is None:
            raise errors.bad_request.InvalidProjectId(id=project_id)
        if role in ("admin", "owner"):
            return

        is_own = object_owner is not None and object_owner == identity.user

        if capability == "read":
            return
        if capability == "owner_only":
            pass
        elif capability == "admin":
            if role in _ADMIN_ROLES:
                return
        elif capability == "write":
            if role == ProjectRole.collaborator or (role == ProjectRole.read_write and is_own):
                return
        elif capability == "delete":
            if role in (ProjectRole.collaborator, ProjectRole.read_write) and is_own:
                return

        raise errors.forbidden.NoWritePermission(
            f"role '{role}' does not have '{capability}' access to project {project_id}"
        )
