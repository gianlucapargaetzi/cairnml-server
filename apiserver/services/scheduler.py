"""Resource-aware scheduling endpoints. The rules are in bll/scheduler; here are the checks of who may call."""

from apiserver.apierrors import errors
from apiserver.bll.scheduler import service
from apiserver.bll.scheduler.model import validate_priority
from apiserver.database.model.scheduler import Reservation, ReservationState, SchedulerEvent, SchedulerWorker
from apiserver.database.model.task.task import Task
from apiserver.idp.tokens import device_of_bearer
from apiserver.service_repo import APICall, endpoint


def _agent_device(call: APICall) -> str:
    device = device_of_bearer(call.authorization)
    if not device:
        raise errors.forbidden.NoWritePermission(reason="only an agent device may call this")
    return device


def _is_admin(call: APICall) -> bool:
    return call.identity.role in ("admin", "root", "system")


@endpoint("scheduler.register_worker", min_version="2.36")
def register_worker(call: APICall, company_id, _):
    body = call.data
    device = _agent_device(call)
    worker = service.register_worker(
        company=company_id,
        device=device,
        name=body["worker"],
        capacity=body["capacity"],
        reserved=body.get("reserved") or {},
        can_enforce=body.get("can_enforce") or {},
        agent_version=body.get("agent_version") or "",
    )
    call.result.data = {"id": worker.id, "state": worker.state}


@endpoint("scheduler.sync", min_version="2.36")
def sync(call: APICall, company_id, _):
    body = call.data
    device = _agent_device(call)
    result = service.sync(
        company=company_id,
        device=device,
        worker_name=body["worker"],
        jobs=body.get("jobs") or [],
        usage=body.get("usage") or {},
    )
    call.result.data = result


@endpoint("scheduler.claim", min_version="2.36")
def claim(call: APICall, company_id, _):
    body = call.data
    device = _agent_device(call)
    call.result.data = service.claim(company_id, device, body["worker"], body["task"])


@endpoint("scheduler.release", min_version="2.36")
def release(call: APICall, company_id, _):
    body = call.data
    device = _agent_device(call)
    call.result.data = service.release(
        company_id,
        device,
        body["worker"],
        body["task"],
        body["outcome"],
        failure=body.get("failure"),
    )


@endpoint("scheduler.get_overview", min_version="2.36")
def get_overview(call: APICall, company_id, _):
    workers = []
    live = service._live_reservations(company_id)
    by_worker = {}
    for r in live:
        by_worker.setdefault(r.worker, []).append(r)
    names = {t.id: t.name for t in Task.objects(id__in=[r.task for r in live]).only("id", "name")}
    for w in SchedulerWorker.objects(company=company_id):
        jobs = by_worker.get(w.id, [])
        workers.append({
            "id": w.id,
            "state": w.state,
            "capacity": w.capacity.to_mongo().to_dict(),
            "reserved": w.reserved.to_mongo().to_dict() if w.reserved else {},
            "usage": w.usage or {},
            "exclusive_task": w.exclusive_task,
            "last_sync": w.last_sync,
            "jobs": [
                {
                    "task": r.task,
                    "name": names.get(r.task, ""),
                    "state": r.state,
                    "cpus": r.cpus,
                    "gpus": r.gpus,
                    "ram_gb": r.ram_gb,
                    "disk_gb": r.disk_gb,
                    "exclusive": r.exclusive,
                    "usage": r.usage or {},
                }
                for r in jobs
            ],
        })
    call.result.data = {"workers": workers}


@endpoint("scheduler.get_jobs", min_version="2.36")
def get_jobs(call: APICall, company_id, _):
    state = (call.data or {}).get("state")
    rows = []
    if state in (None, "running"):
        running = {r.task: r for r in Reservation.objects(company=company_id, state=ReservationState.claimed)}
        for task in Task.objects(id__in=list(running)).only("id", "name", "project", "user", "priority"):
            r = running[task.id]
            rows.append({
                "task": task.id,
                "name": task.name,
                "state": "running",
                "worker": r.worker,
                "priority": task.priority,
                "exclusive": r.exclusive,
                "cpus": r.cpus,
                "gpus": r.gpus,
                "ram_gb": r.ram_gb,
                "disk_gb": r.disk_gb,
                "usage": r.usage or {},
                "since": r.claimed_at,
            })
    if state in (None, "pending"):
        reasons = service.pending_reasons(company_id)
        for task in Task.objects(id__in=list(reasons)).only("id", "name", "priority", "requirements", "created"):
            rows.append({
                "task": task.id,
                "name": task.name,
                "state": "pending",
                "priority": task.priority,
                "requirements": task.requirements or None,
                "reason": reasons[task.id],
                "since": task.created,
            })
    call.result.data = {"jobs": rows}


@endpoint("scheduler.get_events", min_version="2.36")
def get_events(call: APICall, company_id, _):
    limit = min(int((call.data or {}).get("limit") or 100), 500)
    events = SchedulerEvent.objects(company=company_id).order_by("-time").limit(limit)
    call.result.data = {"events": [
        {"time": e.time, "kind": e.kind, "task": e.task, "worker": e.worker, "detail": e.detail} for e in events
    ]}


@endpoint("scheduler.set_priority", min_version="2.36")
def set_priority(call: APICall, company_id, _):
    body = call.data
    priority = validate_priority(body["priority"])
    service.set_priority(company_id, body["task"], priority, user=call.identity.user, is_admin=_is_admin(call))
    call.result.data = {"task": body["task"], "priority": priority}
