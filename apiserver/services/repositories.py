"""The code repositories runs use: the list of the user's repositories, and the GitHub App attached to each one."""

from apiserver.apierrors import errors
from apiserver.bll import repositories as bll
from apiserver.database.model.auth import Role
from apiserver.idp.audit import client_address, record as audit
from apiserver.service_repo import APICall, endpoint


def _is_admin(call: APICall) -> bool:
    return call.identity.role in (Role.admin, Role.root)


@endpoint("repositories.get_all")
def get_all(call: APICall, company: str, _request):
    call.result.data = {"repositories": bll.list_for(company, call.identity.user, _is_admin(call))}


@endpoint("repositories.set_credential")
def set_credential(call: APICall, company: str, _request):
    data = call.data or {}
    credential = data.get("credential") or None
    repository = bll.set_credential(company, call.identity.user, data.get("repository", ""), credential, _is_admin(call))
    audit(call.identity.user, "repository.credential_set", ip=client_address(call), company=company,
          repo=data.get("repository", ""), credential=credential or "")
    call.result.data = {"repository": repository}
