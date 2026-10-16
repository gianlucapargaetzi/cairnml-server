"""The scheduler's state changes: worker registration and sync, offers, claims, releases, lost workers,
and the placement pass that makes offers. Every change of a task's status goes through the existing
status machinery (ChangeStatusRequest), so the task history stays the same as for any other task."""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from apiserver.apierrors import errors
from apiserver.bll.scheduler.ledger import views_for
from apiserver.bll.scheduler.placement import PendingTask, plan, reason_for
from apiserver.bll.task.utils import ChangeStatusRequest
from apiserver.config_repo import config
from apiserver.database.model.scheduler import (
    Reservation,
    ReservationState,
    SchedulerEvent,
    SchedulerWorker,
    WorkerState,
)
from apiserver.database.model.device import Device
from apiserver.idp.audit import record as audit
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.redis_manager import redman

DEFAULT_PRIORITY = 50
LIVE = (ReservationState.offered, ReservationState.claimed)


def _settings():
    return config.get("services.scheduler")


def _now() -> datetime:
    return datetime.utcnow()


def _worker_view_doc(worker: SchedulerWorker) -> dict:
    cap = worker.capacity
    res = worker.reserved
    return {
        "id": worker.id,
        "state": worker.state,
        "capacity": {
            "cpus": cap.cpus,
            "ram_gb": cap.ram_gb,
            "disk_gb": cap.disk_gb,
            "gpus": [dict(g) for g in (cap.gpus or [])],
        },
        "reserved": {
            "cpus": res.cpus if res else 0,
            "ram_gb": res.ram_gb if res else 0,
            "disk_gb": res.disk_gb if res else 0,
        },
        "can_enforce": dict(worker.can_enforce or {}),
    }


@contextmanager
def _company_lock(company: str):
    """One placement pass per company at a time, across all API server processes."""
    lock = redman.connection("apiserver").lock(f"scheduler:{company}", timeout=30, blocking_timeout=10)
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def record_event(company: str, kind: str, task: str = None, worker: str = None, detail: str = "", now=None):
    SchedulerEvent(
        time=now or _now(), company=company, kind=kind, task=task, worker=worker, detail=detail[:500]
    ).save()


def _live_reservations(company: str, worker: str = None):
    query = dict(company=company, state__in=LIVE)
    if worker:
        query["worker"] = worker
    return list(Reservation.objects(**query))


def _pending_tasks(company: str) -> List[PendingTask]:
    live_tasks = {r.task for r in _live_reservations(company)}
    result = []
    for task in Task.objects(company=company, status=TaskStatus.queued).only(
        "id", "priority", "requirements", "created"
    ):
        if task.id in live_tasks:
            continue
        requirements = task.requirements or None
        result.append(
            PendingTask(
                id=task.id,
                priority=int(task.priority if task.priority is not None else DEFAULT_PRIORITY),
                submitted=task.created or _now(),
                requirements=requirements,
            )
        )
    return result


def register_worker(company, device, name, capacity: dict, reserved: dict, can_enforce: dict, agent_version: str):
    existing = SchedulerWorker.objects(id=name).first()
    if existing and existing.device != device:
        raise errors.forbidden.NoWritePermission(worker=name)
    now = _now()
    doc = dict(
        company=company,
        device=device,
        capacity=capacity,
        reserved=reserved,
        can_enforce=can_enforce,
        agent_version=agent_version,
        last_sync=now,
    )
    if existing:
        existing.update(**doc, state=WorkerState.active)
        return SchedulerWorker.objects(id=name).first()
    worker = SchedulerWorker(id=name, state=WorkerState.active, **doc)
    worker.save()
    record_event(company, "worker_registered", worker=name, now=now)
    audit(device, "scheduler.worker_registered", worker=name, company=company)
    return worker



def _user_of_device(device: str) -> str:
    dev = Device.objects(id=device).only("user").first()
    return dev.user if dev else ""


def _worker_of_device(name: str, device: str) -> SchedulerWorker:
    worker = SchedulerWorker.objects(id=name).first()
    if not worker or worker.device != device:
        raise errors.forbidden.NoWritePermission(worker=name)
    return worker


def expire_offers(company: str, now: datetime):
    expiry = timedelta(seconds=int(_settings().get("offer_expiry_sec", 60)))
    for r in Reservation.objects(company=company, state=ReservationState.offered, offered_at__lt=now - expiry):
        r.update(state=ReservationState.released, released_at=now, release_reason="offer expired")
        record_event(company, "released", task=r.task, worker=r.worker, detail="offer expired", now=now)


def mark_lost_workers(company: str, now: datetime) -> List[str]:
    settings = _settings()
    limit = timedelta(seconds=int(settings.get("sync_interval_sec", 10)) * int(settings.get("lost_after_missed_syncs", 3)))
    lost = []
    for worker in SchedulerWorker.objects(company=company, state=WorkerState.active, last_sync__lt=now - limit):
        worker.update(state=WorkerState.lost)
        lost.append(worker.id)
        record_event(company, "worker_lost", worker=worker.id, now=now)
        for r in Reservation.objects(worker=worker.id, state__in=LIVE):
            _lose_reservation(r, now, user_id=_user_of_device(worker.device))
    return lost


def _lose_reservation(r: Reservation, now: datetime, user_id: str):
    was_claimed = r.state == ReservationState.claimed
    r.update(state=ReservationState.released, released_at=now, release_reason="worker lost")
    if not was_claimed:
        return
    attempts = Reservation.objects(task=r.task, release_reason="worker lost").count()
    max_attempts = int(_settings().get("max_attempts", 2))
    task = Task.objects(id=r.task).only("id", "status", "company").first()
    if not task:
        return
    if attempts >= max_attempts:
        _fail_task(task, now, reason="worker_lost",
                   detail=f"Failure due to a lost worker: {r.worker} stopped responding, twice",
                   worker=r.worker, user_id=user_id)
        record_event(task.company, "failed", task=task.id, worker=r.worker, detail="worker lost twice", now=now)
    else:
        ChangeStatusRequest(
            task=task, new_status=TaskStatus.queued, force=True, user_id=user_id,
            status_reason="worker lost", status_message=f"worker {r.worker} lost; retrying once",
        ).execute()
        record_event(task.company, "retried", task=task.id, worker=r.worker, detail="worker lost", now=now)


def _fail_task(task: Task, now: datetime, reason: str, detail: str, user_id: str, worker: str = "",
               exit_code=None, reserved=None, peak=None):
    failure = {"reason": reason, "detail": detail, "worker": worker, "time": now.isoformat()}
    if exit_code is not None:
        failure["exit_code"] = exit_code
    if reserved:
        failure["reserved"] = reserved
    if peak:
        failure["peak"] = peak
    ChangeStatusRequest(
        task=task, new_status=TaskStatus.failed, force=True, user_id=user_id,
        status_reason=reason, status_message=detail,
    ).execute(completed=now, failure=failure)


def placement_pass(company: str, now: Optional[datetime] = None) -> int:
    """Make offers for the pending tasks that fit. Returns the number of offers made."""
    now = now or _now()
    with _company_lock(company):
        expire_offers(company, now)
        mark_lost_workers(company, now)
        workers = [w for w in SchedulerWorker.objects(company=company)]
        reservations = _live_reservations(company)
        pending = _pending_tasks(company)
        if not pending:
            return 0
        views = views_for([_worker_view_doc(w) for w in workers if w.state == WorkerState.active],
                          [r.to_mongo().to_dict() for r in reservations])
        result = plan(pending, views)
        for p in result.placements:
            Reservation(
                id=uuid.uuid4().hex,
                task=p.task,
                worker=p.worker,
                company=company,
                state=ReservationState.offered,
                cpus=p.cpus,
                gpus=p.gpus,
                ram_gb=p.ram_gb,
                disk_gb=p.disk_gb,
                exclusive=p.exclusive,
                offered_at=now,
            ).save()
            record_event(company, "placed", task=p.task, worker=p.worker, now=now)
        return len(result.placements)


def claim(company: str, device: str, worker_name: str, task_id: str, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    worker = _worker_of_device(worker_name, device)
    expiry = timedelta(seconds=int(_settings().get("offer_expiry_sec", 60)))
    r = Reservation.objects(task=task_id, worker=worker.id, state=ReservationState.offered).first()
    if not r:
        raise errors.bad_request.ValidationError("no offer for this task on this worker")
    if r.offered_at < now - expiry:
        expire_offers(company, now)
        raise errors.bad_request.ValidationError("the offer has expired")
    task = Task.objects(id=task_id).only("id", "status", "company").first()
    if not task or task.status != TaskStatus.queued:
        r.update(state=ReservationState.released, released_at=now, release_reason="task is no longer queued")
        raise errors.bad_request.ValidationError("the task is no longer queued")

    r.update(state=ReservationState.claimed, claimed_at=now)
    ChangeStatusRequest(
        task=task, new_status=TaskStatus.in_progress, force=True, user_id=_user_of_device(device),
        status_reason="launch by agent", status_message=f"claimed by worker {worker.id}",
    ).execute(started=now, unset__completed=1, unset__active_duration=1)
    Task.objects(id=task_id).update_one(set__last_device=device)
    if r.exclusive:
        worker.update(exclusive_task=task_id)
    record_event(company, "claimed", task=task_id, worker=worker.id, now=now)
    audit(device, "scheduler.claimed", task=task_id, worker=worker.id, exclusive=r.exclusive)
    return {"task": task_id, "reservation": r.id}


def release(company: str, device: str, worker_name: str, task_id: str, outcome: str,
            failure: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    """The job ended. `outcome` is completed, stopped or failed; a failure carries its structured reason."""
    now = now or _now()
    worker = _worker_of_device(worker_name, device)
    r = Reservation.objects(task=task_id, worker=worker.id, state=ReservationState.claimed).first()
    if r:
        r.update(state=ReservationState.released, released_at=now, release_reason=outcome)
        if worker.exclusive_task == task_id:
            worker.update(exclusive_task=None)
    else:
        # a sync may already have cancelled it (the process marks the task stopped as it is killed); the
        # agent still reports how the job ended, and a failure must be recorded
        cancelled = Reservation.objects(task=task_id, worker=worker.id, state=ReservationState.released,
                                        release_reason="cancelled").order_by("-released_at").first()
        if not cancelled or outcome != "failed":
            raise errors.bad_request.ValidationError("no running job for this task on this worker")

    task = Task.objects(id=task_id).only("id", "status", "company").first()
    if outcome == "failed" and failure:
        _fail_task(task, now, reason=failure["reason"], detail=failure["detail"], worker=worker.id,
                   user_id=_user_of_device(device),
                   exit_code=failure.get("exit_code"), reserved=failure.get("reserved"), peak=failure.get("peak"))
    elif outcome == "completed":
        ChangeStatusRequest(task=task, new_status=TaskStatus.completed, force=True,
                            user_id=_user_of_device(device)).execute(completed=now)
    elif outcome == "stopped":
        ChangeStatusRequest(task=task, new_status=TaskStatus.stopped, force=True,
                            user_id=_user_of_device(device)).execute(completed=now)
    record_event(company, "released", task=task_id, worker=worker.id, detail=outcome, now=now)
    audit(device, "scheduler.released", task=task_id, worker=worker.id, outcome=outcome,
          reason=(failure or {}).get("reason", ""))
    return {"task": task_id, "outcome": outcome}


def sync(company: str, device: str, worker_name: str, jobs: Iterable[dict], usage: dict,
         now: Optional[datetime] = None) -> dict:
    """Heartbeat from the agent. Returns the offers for this worker, and the running jobs the server
    no longer expects (the agent stops those containers)."""
    now = now or _now()
    jobs = list(jobs or [])
    worker = _worker_of_device(worker_name, device)
    was_lost = worker.state == WorkerState.lost
    worker.update(last_sync=now, usage=usage or {}, state=WorkerState.active)
    if was_lost:
        record_event(company, "worker_back", worker=worker.id, now=now)

    usage_by_task = {j["task"]: j.get("usage") or {} for j in jobs or [] if j.get("task")}
    for task_id, job_usage in usage_by_task.items():
        Reservation.objects(task=task_id, worker=worker.id, state=ReservationState.claimed).update(
            set__usage=job_usage
        )

    # a claimed job whose task is no longer running (stopped by a user, or changed elsewhere) ends here
    cancelled = []
    for r in Reservation.objects(worker=worker.id, state=ReservationState.claimed):
        task = Task.objects(id=r.task).only("status").first()
        if task is not None and task.status == TaskStatus.in_progress:
            continue
        r.update(state=ReservationState.released, released_at=now, release_reason="cancelled")
        if worker.exclusive_task == r.task:
            worker.update(exclusive_task=None)
        record_event(company, "cancelled", task=r.task, worker=worker.id, now=now)
        cancelled.append(r.task)

    # after the cancelled ones: a claimed job the agent does not report as running was lost on the machine (the agent crashed
    # before starting it, or the container died): retried once, like a lost worker
    reported = {j["task"] for j in (jobs or []) if j.get("task")}
    for r in Reservation.objects(worker=worker.id, state=ReservationState.claimed):
        if r.task not in reported:
            _lose_reservation(r, now, user_id=_user_of_device(device))

    expected = {r.task for r in _live_reservations(company, worker=worker.id) if r.state == ReservationState.claimed}
    reported = set(usage_by_task)
    stale = sorted((reported - expected) | set(cancelled))

    placement_pass(company, now)
    offers = []
    for r in Reservation.objects(worker=worker.id, state=ReservationState.offered):
        task = Task.objects(id=r.task).only("requirements").first()
        max_runtime = ((task.requirements or {}).get("max_runtime_min") if task else None)
        offers.append({
            "task": r.task,
            "cpus": r.cpus,
            "gpus": r.gpus,
            "ram_gb": r.ram_gb,
            "disk_gb": r.disk_gb,
            "exclusive": r.exclusive,
            "max_runtime_min": max_runtime,
        })
    return {"offers": offers, "stop": stale}


def set_priority(company: str, task_id: str, priority: int, user: str, is_admin: bool) -> None:
    task = Task.objects(id=task_id, company=company).only("id", "user").first()
    if not task:
        raise errors.bad_request.InvalidTaskId(task=task_id)
    if task.user != user and not is_admin:
        raise errors.forbidden.NoWritePermission(task=task_id)
    Task.objects(id=task_id).update_one(set__priority=priority)
    record_event(company, "priority_changed", task=task_id, detail=f"priority {priority}")
    audit(user, "scheduler.priority_changed", task=task_id, priority=priority)
    placement_pass(company)


def pending_reasons(company: str) -> dict:
    """Why each pending task is not placed now, computed the same way as the placement pass."""
    workers = [w for w in SchedulerWorker.objects(company=company) if w.state == WorkerState.active]
    views = views_for([_worker_view_doc(w) for w in workers],
                      [r.to_mongo().to_dict() for r in _live_reservations(company)])
    pending = _pending_tasks(company)
    result = plan(pending, views)
    reasons = dict(result.reasons)
    placed = {p.task for p in result.placements}
    return {t.id: reasons.get(t.id) or reason_for(t, views) for t in pending if t.id not in placed}
