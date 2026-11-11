"""The code repositories runs use: one row per repository, a binding per project that uses it, and the credential
assigned to each binding. See bll/repositories."""

from apiserver.apierrors import errors
from apiserver.bll import repositories as bll
from apiserver.database.model.auth import Role
from apiserver.idp.audit import client_address, record as audit
from apiserver.service_repo import APICall, endpoint


def _is_admin(call: APICall) -> bool:
    return call.identity.role in (Role.admin, Role.root)


@endpoint("repositories.get_all")
def get_all(call: APICall, company: str, _request):
    project = (call.data or {}).get("project") or None
    call.result.data = {"repositories": bll.list_for(company, call.identity.user, _is_admin(call), project)}


@endpoint("repositories.add")
def add(call: APICall, company: str, _request):
    data = call.data or {}
    binding = bll.add(company, call.identity.user, _is_admin(call), data.get("repository", ""), data.get("project") or None)
    audit(call.identity.user, "repository.added", ip=client_address(call), company=company,
          repo=data.get("repository", ""), project=binding.project or "")
    call.result.data = {"binding": binding.id}


@endpoint("repositories.set_credential")
def set_credential(call: APICall, company: str, _request):
    data = call.data or {}
    binding_id = data.get("binding")
    if not binding_id:
        raise errors.bad_request.ValidationError("binding is required")
    credential = data.get("credential") or None
    none_needed = bool(data.get("none_needed"))
    binding = bll.set_credential(company, call.identity.user, _is_admin(call), binding_id, credential, none_needed)
    audit(call.identity.user, "repository.credential_set", ip=client_address(call), company=company,
          binding=binding.id, credential=credential or "", none_needed=none_needed)
    call.result.data = {"binding": binding.id}


@endpoint("repositories.check")
def check(call: APICall, company: str, _request):
    binding_id = (call.data or {}).get("binding")
    if not binding_id:
        raise errors.bad_request.ValidationError("binding is required")
    view = bll.check_binding(company, call.identity.user, _is_admin(call), binding_id)
    audit(call.identity.user, "repository.checked", ip=client_address(call), company=company,
          binding=binding_id, state=view["state"])
    call.result.data = {"binding": view}


@endpoint("repositories.delete")
def delete(call: APICall, company: str, _request):
    binding_id = (call.data or {}).get("binding")
    if not binding_id:
        raise errors.bad_request.ValidationError("binding is required")
    bll.delete_binding(company, call.identity.user, _is_admin(call), binding_id)
    audit(call.identity.user, "repository.deleted", ip=client_address(call), company=company, binding=binding_id)
    call.result.data = {"deleted": True}
