"""The GitHub App status: whether the App works and what it reaches. See bll/github_status.

Every signed-in user gets the state and a one-line message for the banner. The reasons, the steps and the repositories
are for admins only, as only an admin can fix the key.
"""

from apiserver.apierrors import errors
from apiserver.bll import github_status as status_bll
from apiserver.database.model.auth import Role
from apiserver.service_repo import APICall, endpoint

_ADMIN_ROLES = (Role.admin, Role.root)
_NOT_CHECKED = {"state": "unknown", "message": "The GitHub App has not been checked yet.", "steps": []}


def _is_admin(call: APICall) -> bool:
    return call.identity.role in _ADMIN_ROLES


@endpoint("github.status")
def status(call: APICall, _company: str, _request):
    result = status_bll.get_status() or _NOT_CHECKED
    if not _is_admin(call):
        result = status_bll.user_view(result)
    call.result.data = {"status": result}


@endpoint("github.check")
def check(call: APICall, _company: str, _request):
    if not _is_admin(call):
        raise errors.forbidden.RoleNotAllowed("only an admin may check the GitHub App")
    call.result.data = {"status": status_bll.save_check()}
