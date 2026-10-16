"""Placement of pending tasks on workers. Pure: no database, no clock, so it can be tested exhaustively.

Rules (see docs/clearml/scheduler-concept.md):
- Tasks are taken by priority (higher first), then by submission time.
- A task with requirements goes to a worker with enough free CPUs, RAM, disk and matching GPUs. The
  worker must be able to enforce limits (Docker). The choice is the best fit, so large holes stay open.
- A task with no requirements is exclusive: it needs a worker with nothing else running, and then
  blocks that worker until it ends.
- The first cpus and the first part of the RAM and disk are kept for the host and never placed.
- A task that does not fit now is skipped (backfill), and it gets a reason.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set


@dataclass
class WorkerView:
    id: str
    state: str
    cpus: int
    ram_gb: float
    disk_gb: float
    gpus: List[dict]
    reserved_cpus: int = 0
    reserved_ram_gb: float = 0
    reserved_disk_gb: float = 0
    can_enforce: Dict[str, bool] = field(default_factory=dict)
    exclusive_busy: bool = False
    running_reservations: int = 0
    allocated_cpus: Set[int] = field(default_factory=set)
    allocated_ram_gb: float = 0
    allocated_disk_gb: float = 0
    allocated_gpus: Set[int] = field(default_factory=set)

    @property
    def schedulable_cpu_ids(self) -> List[int]:
        return list(range(self.reserved_cpus, self.cpus))

    @property
    def schedulable_ram_gb(self) -> float:
        return self.ram_gb - self.reserved_ram_gb

    @property
    def schedulable_disk_gb(self) -> float:
        return self.disk_gb - self.reserved_disk_gb

    def free_cpu_ids(self) -> List[int]:
        return [c for c in self.schedulable_cpu_ids if c not in self.allocated_cpus]

    def free_ram_gb(self) -> float:
        return self.schedulable_ram_gb - self.allocated_ram_gb

    def free_disk_gb(self) -> float:
        return self.schedulable_disk_gb - self.allocated_disk_gb

    def free_gpus(self) -> List[dict]:
        return [g for g in self.gpus if g["index"] not in self.allocated_gpus]

    def accepting_shared(self) -> bool:
        return (
            self.state == "active"
            and not self.exclusive_busy
            and bool(self.can_enforce.get("docker"))
        )

    def accepting_exclusive(self) -> bool:
        return self.state == "active" and not self.exclusive_busy and self.running_reservations == 0


@dataclass
class PendingTask:
    id: str
    priority: int
    submitted: datetime
    requirements: Optional[dict]
    """ None means exclusive: the task needs a whole machine """


@dataclass
class Placement:
    task: str
    worker: str
    cpus: List[int]
    gpus: List[int]
    ram_gb: float
    disk_gb: float
    exclusive: bool


@dataclass
class PlanResult:
    placements: List[Placement]
    reasons: Dict[str, str]


def _gpu_matches(gpu: dict, wanted: dict) -> bool:
    if gpu["memory_gb"] < wanted.get("min_memory_gb", 0):
        return False
    model = wanted.get("model")
    return not model or gpu.get("model") == model


def _fits_shared(w: WorkerView, req: dict) -> bool:
    if not w.accepting_shared():
        return False
    if len(w.free_cpu_ids()) < req["cpus"]:
        return False
    if w.free_ram_gb() < req["ram_gb"] or w.free_disk_gb() < req["disk_gb"]:
        return False
    matching = [g for g in w.free_gpus() if _gpu_matches(g, req["gpus"])]
    return len(matching) >= req["gpus"]["count"]


def _best_fit_key(w: WorkerView, req: dict):
    return (w.free_ram_gb() - req["ram_gb"], len(w.free_cpu_ids()) - req["cpus"], w.id)


def _allocate_shared(w: WorkerView, task: PendingTask, req: dict) -> Placement:
    cpus = w.free_cpu_ids()[: req["cpus"]]
    gpus = [g["index"] for g in w.free_gpus() if _gpu_matches(g, req["gpus"])][: req["gpus"]["count"]]
    w.allocated_cpus |= set(cpus)
    w.allocated_gpus |= set(gpus)
    w.allocated_ram_gb += req["ram_gb"]
    w.allocated_disk_gb += req["disk_gb"]
    w.running_reservations += 1
    return Placement(task.id, w.id, cpus, gpus, req["ram_gb"], req["disk_gb"], exclusive=False)


def _allocate_exclusive(w: WorkerView, task: PendingTask) -> Placement:
    cpus = w.schedulable_cpu_ids
    gpus = [g["index"] for g in w.gpus]
    w.allocated_cpus |= set(cpus)
    w.allocated_gpus |= set(gpus)
    w.allocated_ram_gb = w.schedulable_ram_gb
    w.allocated_disk_gb = w.schedulable_disk_gb
    w.exclusive_busy = True
    w.running_reservations += 1
    return Placement(task.id, w.id, cpus, gpus, w.schedulable_ram_gb, w.schedulable_disk_gb, exclusive=True)


def _largest_possible(workers: List[WorkerView], req: dict) -> bool:
    """Could any worker ever run this, if it were empty?"""
    for w in workers:
        if w.state != "active" or not w.can_enforce.get("docker"):
            continue
        if len(w.schedulable_cpu_ids) < req["cpus"] or w.schedulable_ram_gb < req["ram_gb"]:
            continue
        if w.schedulable_disk_gb < req["disk_gb"]:
            continue
        matching = [g for g in w.gpus if _gpu_matches(g, req["gpus"])]
        if len(matching) >= req["gpus"]["count"]:
            return True
    return False


def reason_for(task: PendingTask, workers: List[WorkerView]) -> str:
    """A short sentence on why the task is not placed now."""
    if task.requirements is None:
        if not any(w.state == "active" for w in workers):
            return "waiting for a worker; none is active"
        return "waiting for a whole machine; every worker is busy"

    req = task.requirements
    if not _largest_possible(workers, req):
        if req["gpus"]["count"] and not any(
            _gpu_matches(g, req["gpus"]) for w in workers for g in w.gpus
        ):
            return (
                f"needs {req['gpus']['count']} GPU with {req['gpus']['min_memory_gb']:g} GB memory"
                f"{' (' + req['gpus']['model'] + ')' if req['gpus']['model'] else ''}; no worker has one"
            )
        return (
            f"no worker is large enough for {req['cpus']} CPUs, {req['ram_gb']:g} GB RAM "
            f"and {req['disk_gb']:g} GB disk"
        )
    return (
        f"waiting for free resources: needs {req['cpus']} CPUs and {req['ram_gb']:g} GB RAM; "
        "no worker has them free now"
    )


def plan(tasks: List[PendingTask], workers: List[WorkerView]) -> PlanResult:
    """Place as many tasks as fit, in priority order. The inputs are copied; they are not changed."""
    views = [
        WorkerView(**{**w.__dict__, "allocated_cpus": set(w.allocated_cpus), "allocated_gpus": set(w.allocated_gpus)})
        for w in workers
    ]
    ordered = sorted(tasks, key=lambda t: (-t.priority, t.submitted, t.id))
    placements: List[Placement] = []
    reasons: Dict[str, str] = {}

    for task in ordered:
        if task.requirements is None:
            candidates = [w for w in views if w.accepting_exclusive()]
            if not candidates:
                reasons[task.id] = reason_for(task, views)
                continue
            chosen = min(candidates, key=lambda w: (len(w.schedulable_cpu_ids), w.id))
            placements.append(_allocate_exclusive(chosen, task))
            continue

        candidates = [w for w in views if _fits_shared(w, task.requirements)]
        if not candidates:
            reasons[task.id] = reason_for(task, views)
            continue
        chosen = min(candidates, key=lambda w: _best_fit_key(w, task.requirements))
        placements.append(_allocate_shared(chosen, task, task.requirements))

    return PlanResult(placements=placements, reasons=reasons)
