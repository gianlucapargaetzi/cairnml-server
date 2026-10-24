from typing import Optional

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
