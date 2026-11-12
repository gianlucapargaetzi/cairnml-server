# apiserver/services/access.py
"""The shares of queues, datasets, registries and credentials (see bll/access). The checks on those resources read them."""

from apiserver.bll import access as access_bll
from apiserver.bll.repositories import prune_assignments
from apiserver.database.model.share import Share
from apiserver.service_repo import APICall, endpoint


def _view(record: Share) -> dict:
    return {
        "id": record.id,
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "scope": record.scope,
        "project": record.project or "",
        "user": record.user or "",
        "level": record.level,
        "granted_by": record.granted_by,
        "inherited": bool(record.inherited),
        "created": record.created.isoformat() if record.created else None,
    }


@endpoint("access.list")
def list_shares(call: APICall, company: str, _request):
    data = call.data or {}
    shares = access_bll.list_shares(
        company, call.identity.user, data.get("resource_type", ""), data.get("resource_id", ""))
    call.result.data = {"shares": [_view(record) for record in shares]}


@endpoint("access.share")
def share(call: APICall, company: str, _request):
    data = call.data or {}
    record = access_bll.share(
        company, call.identity.user, data.get("resource_type", ""), data.get("resource_id", ""),
        data.get("scope", ""), data.get("project") or "", data.get("level", ""), data.get("user") or "")
    call.result.data = {"share": _view(record)}


@endpoint("access.unshare")
def unshare(call: APICall, company: str, _request):
    data = call.data or {}
    share = Share.objects(company=company, id=data.get("share_id", "")).only("resource_type", "resource_id").first()
    access_bll.unshare(company, call.identity.user, data.get("share_id", ""))
    if share is not None and share.resource_type == "repository":
        prune_assignments(company, share.resource_id)  # who no longer sees the repository holds no assignment on it
    call.result.data = {"deleted": True}
