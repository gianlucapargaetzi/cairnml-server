"""Scheduler state: one document per machine (worker), its reservations, and the event log.

Free capacity is never stored. It is computed from the reservations that are offered or claimed,
so it cannot drift from them.
"""

from mongoengine import (
    BooleanField,
    DateTimeField,
    DictField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentField,
    FloatField,
    IntField,
    ListField,
    StringField,
)

from apiserver.database import Database, strict
from apiserver.database.model import DbModelMixin


class WorkerState(object):
    active = "active"
    draining = "draining"
    lost = "lost"


class ReservationState(object):
    offered = "offered"
    """ Placed by the scheduler; the agent has not claimed it yet """
    claimed = "claimed"
    """ The agent took it and the job is running """
    released = "released"


class Capacity(EmbeddedDocument):
    cpus = IntField(required=True, min_value=0)
    ram_gb = FloatField(required=True, min_value=0)
    disk_gb = FloatField(default=0, min_value=0)
    gpus = ListField(DictField(), default=list)
    """ [{index, model, memory_gb}] """


class SchedulerWorker(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "scheduler_workers",
        "indexes": ["company", ("company", "state")],
    }

    id = StringField(primary_key=True)
    """ The worker name; one per machine """
    company = StringField(required=True)
    device = StringField(required=True)
    """ The agent device that registered the worker. Only this device may sync it. """
    state = StringField(default=WorkerState.active, choices=(WorkerState.active, WorkerState.draining, WorkerState.lost))
    capacity = EmbeddedDocumentField(Capacity, required=True)
    reserved = EmbeddedDocumentField(Capacity)
    """ The part of the machine kept back for the operating system and host services """
    can_enforce = DictField(default=dict)
    """ What the agent can enforce: {docker, gpu_device_limit, cpuset} """
    exclusive_task = StringField()
    """ Set while a job that blocks the whole worker runs """
    last_sync = DateTimeField()
    agent_version = StringField(default="")


class Reservation(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "scheduler_reservations",
        "indexes": [
            "task",
            "worker",
            ("worker", "state"),
            ("company", "state"),
        ],
    }

    id = StringField(primary_key=True)
    task = StringField(required=True)
    worker = StringField(required=True)
    company = StringField(required=True)
    state = StringField(required=True, choices=(
        ReservationState.offered, ReservationState.claimed, ReservationState.released,
    ))
    cpus = ListField(IntField(), default=list)
    """ CPU ids chosen for the job """
    ram_gb = FloatField(default=0, min_value=0)
    gpus = ListField(IntField(), default=list)
    """ GPU indices chosen for the job """
    disk_gb = FloatField(default=0, min_value=0)
    exclusive = BooleanField(default=False)
    offered_at = DateTimeField()
    claimed_at = DateTimeField()
    released_at = DateTimeField()
    release_reason = StringField(default="")


class SchedulerEvent(DbModelMixin, Document):
    meta = {
        "db_alias": Database.backend,
        "strict": strict,
        "collection": "scheduler_events",
        "indexes": [("company", "-time"), "task"],
    }

    time = DateTimeField(required=True)
    company = StringField(required=True)
    kind = StringField(required=True)
    """ placed, claimed, released, failed, retried, worker_lost, worker_back, priority_changed, cancelled """
    task = StringField()
    worker = StringField()
    detail = StringField(default="")
