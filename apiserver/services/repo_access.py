"""How repositories are reached by a project, and what a dispatch needs; see bll/repo_access. Read-only."""

from apiserver.apierrors import errors
from apiserver.bll import repo_access as access_bll
from apiserver.database.model.auth import Role
from apiserver.service_repo import APICall, endpoint

MAX_REPOSITORIES = 50


def _is_admin(call: APICall) -> bool:
    return call.identity.role in (Role.admin, Role.root)


@endpoint("repo_access.resolve")
def resolve(call: APICall, company: str, request):
    repositories = (call.data or {}).get("repositories") or []
    if not isinstance(repositories, list) or len(repositories) > MAX_REPOSITORIES:
        raise errors.bad_request.ValidationError(f"give a list of at most {MAX_REPOSITORIES} repositories")
    project = (call.data or {}).get("project") or None
    call.result.data = {"repositories": access_bll.resolve(
        company, [str(item) for item in repositories], call.identity.user, project, _is_admin(call))}


@endpoint("repo_access.requirement")
def requirement(call: APICall, company: str, request):
    tasks = (call.data or {}).get("tasks") or []
    if not isinstance(tasks, list) or len(tasks) > 500:
        raise errors.bad_request.ValidationError("give a list of at most 500 tasks")
    call.result.data = {"repositories": access_bll.requirement(
        company, call.identity.user, [str(item) for item in tasks], _is_admin(call))}
