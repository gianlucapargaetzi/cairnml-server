"""The repository accesses a user owns: their tokens and GitHub Apps. Only the owner sees, checks, changes or deletes
them; the secret is written once and never returned. See bll/user_access."""

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


@endpoint("user_access.add_token")
def add_token(call: APICall, company: str, _request):
    data = call.data or {}
    call.result.data = {"access": access_bll.add_token(
        company, call.identity.user, data.get("label", ""), data.get("host", ""), data.get("path", ""),
        data.get("token", ""))}


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


@endpoint("user_access.grant")
def grant(call: APICall, company: str, _request):
    data = call.data or {}
    call.result.data = {"access": access_bll.grant(
        company, call.identity.user, data.get("id", ""), data.get("project", ""))}


@endpoint("user_access.revoke")
def revoke(call: APICall, company: str, _request):
    data = call.data or {}
    is_admin = call.identity.role in (Role.admin, Role.root)
    call.result.data = {"access": access_bll.revoke(
        company, call.identity.user, data.get("id", ""), data.get("project", ""), is_admin)}


@endpoint("user_access.set_agent_tokens")
def set_agent_tokens(call: APICall, company: str, _request):
    data = call.data or {}
    is_admin = call.identity.role in (Role.admin, Role.root)
    call.result.data = access_bll.set_agent_tokens_allowed(
        company, call.identity.user, data.get("project", ""), bool(data.get("allowed", True)), is_admin)


@endpoint("user_access.project_grants")
def project_grants(call: APICall, company: str, _request):
    project = (call.data or {}).get("project", "")
    grants = access_bll.project_grants(company, call.identity.user, project)
    call.result.data = {
        "grants": grants,
        "agent_tokens_allowed": access_bll.project_agent_tokens_allowed(
            company, access_bll.root_project(company, project)),
    }
