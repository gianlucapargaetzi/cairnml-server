"""Audit events, for the administrators to read: who did what, from which address, and when."""

from datetime import datetime, timezone

from apiserver.apierrors import errors
from apiserver.apimodels.audit import AuditListRequest
from apiserver.database.model.audit import AuditEvent
from apiserver.service_repo import APICall, endpoint

MAX_PAGE_SIZE = 500


def _utc_naive(value: str, name: str):
    if not value:
        return None
    try:
        time = datetime.fromisoformat(value)
    except ValueError:
        raise errors.bad_request.ValidationError(f"{name} must be an ISO 8601 time") from None
    if time.tzinfo is not None:
        time = time.astimezone(timezone.utc).replace(tzinfo=None)
    return time


def _event_view(event: AuditEvent) -> dict:
    return {
        "id": str(event.id),
        "ts": event.ts.isoformat() if event.ts else None,
        "actor": event.actor,
        "action": event.action,
        "ip": event.ip,
        "detail": event.detail,
    }


@endpoint("audit.get_all", min_version="2.8", request_data_model=AuditListRequest)
def get_all(call: APICall, _company, request: AuditListRequest):
    """Newest first. Admins only (see the schema)."""
    query = {}
    if request.actor:
        query["actor"] = request.actor
    if request.action:
        query["action"] = request.action
    since = _utc_naive(request.since, "since")
    if since:
        query["ts__gte"] = since
    until = _utc_naive(request.until, "until")
    if until:
        query["ts__lte"] = until

    page = max(request.page or 0, 0)
    page_size = min(max(request.page_size or 1, 1), MAX_PAGE_SIZE)
    matching = AuditEvent.objects(**query)
    events = matching.order_by("-ts").skip(page * page_size).limit(page_size)
    return {
        "events": [_event_view(event) for event in events],
        "total": matching.count(),
        "page": page,
        "page_size": page_size,
    }
