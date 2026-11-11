"""The repository accesses (credentials) a user owns: their GitHub Apps, and the company's, which the admin owns. Only the owner
sees, checks, changes or deletes them; the secret is written once and never returned. A credential is never shared with a
project: it is used through the assignments of repositories (see bll/repositories). See bll/user_access."""

from apiserver.apierrors import errors
from apiserver.bll import user_access as access_bll
from apiserver.database.model.auth import Role
from apiserver.service_repo import APICall, endpoint


@endpoint("user_access.get_all")
def get_all(call: APICall, company: str, _request):
    is_admin = call.identity.role in (Role.admin, Role.root)
    data = access_bll.list_for(company, call.identity.user)
    data["credentials"] = access_bll.credentials_for(company, call.identity.user, is_admin)
    data["projects"] = access_bll.my_projects(company, call.identity.user)
    call.result.data = data


@endpoint("user_access.add_app")
def add_app(call: APICall, company: str, _request):
    data = call.data or {}
    scope = data.get("scope") or "user"
    if scope == "company" and call.identity.role not in (Role.admin, Role.root):
        raise errors.forbidden.RoleNotAllowed("only an admin may add the company's GitHub App")
    call.result.data = {"access": access_bll.add_app(
        company, call.identity.user, data.get("label", ""), data.get("app_id", ""), data.get("api_url", ""),
        data.get("key", ""), scope=scope)}


@endpoint("user_access.delete")
def delete(call: APICall, company: str, _request):
    is_admin = call.identity.role in (Role.admin, Role.root)
    access_bll.delete(company, call.identity.user, (call.data or {}).get("id", ""), is_admin)
    call.result.data = {"deleted": True}


@endpoint("user_access.check")
def check(call: APICall, company: str, _request):
    is_admin = call.identity.role in (Role.admin, Role.root)
    call.result.data = {"access": access_bll.check(company, call.identity.user, (call.data or {}).get("id", ""), is_admin)}
