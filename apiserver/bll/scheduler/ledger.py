"""Builds the placement view of each worker from its stored capacity and its live reservations.

Free capacity is never stored. It is derived here from the reservations that are offered or
claimed, so it cannot drift from them.
"""

from typing import Iterable, List

from apiserver.bll.scheduler.placement import WorkerView

LIVE_STATES = ("offered", "claimed")


def view_from_docs(worker: dict, reservations: Iterable[dict]) -> WorkerView:
    """`worker` holds id, state, capacity, reserved, can_enforce. `reservations` are this worker's."""
    cap = worker["capacity"]
    reserved = worker.get("reserved") or {}
    view = WorkerView(
        id=worker["id"],
        state=worker.get("state", "active"),
        cpus=int(cap["cpus"]),
        ram_gb=float(cap["ram_gb"]),
        disk_gb=float(cap.get("disk_gb", 0)),
        gpus=list(cap.get("gpus") or []),
        reserved_cpus=int(reserved.get("cpus", 0)),
        reserved_ram_gb=float(reserved.get("ram_gb", 0)),
        reserved_disk_gb=float(reserved.get("disk_gb", 0)),
        can_enforce=dict(worker.get("can_enforce") or {}),
    )
    for r in reservations:
        if r.get("state") not in LIVE_STATES:
            continue
        view.running_reservations += 1
        view.allocated_cpus |= set(r.get("cpus") or [])
        view.allocated_gpus |= set(r.get("gpus") or [])
        view.allocated_ram_gb += float(r.get("ram_gb") or 0)
        view.allocated_disk_gb += float(r.get("disk_gb") or 0)
        if r.get("exclusive"):
            view.exclusive_busy = True
    return view


def views_for(workers: List[dict], reservations: List[dict]) -> List[WorkerView]:
    by_worker = {}
    for r in reservations:
        by_worker.setdefault(r["worker"], []).append(r)
    return [view_from_docs(w, by_worker.get(w["id"], [])) for w in workers]
