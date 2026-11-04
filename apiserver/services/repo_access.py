"""How repositories are reached: publicly, through a GitHub App, or through a token, the user's own or the company's.
Read-only for every signed-in user; see bll/repo_access."""

from apiserver.apierrors import errors
from apiserver.bll import repo_access as access_bll
from apiserver.service_repo import APICall, endpoint

MAX_REPOSITORIES = 50


@endpoint("repo_access.resolve")
def resolve(call: APICall, company: str, request):
    repositories = (call.data or {}).get("repositories") or []
    if not isinstance(repositories, list) or len(repositories) > MAX_REPOSITORIES:
        raise errors.bad_request.ValidationError(f"give a list of at most {MAX_REPOSITORIES} repositories")
    call.result.data = {"repositories": access_bll.resolve(company, [str(item) for item in repositories], call.identity.user)}
