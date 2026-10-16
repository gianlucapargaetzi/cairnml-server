from datetime import datetime
from unittest import TestCase

from mongoengine.errors import ValidationError

from apiserver.database.model.scheduler import (
    SchedulerWorker,
    Reservation,
    ReservationState,
    SchedulerEvent,
)


class TestSchedulerModels(TestCase):
    def test_worker_defaults_to_active_and_empty_allocation(self):
        worker = SchedulerWorker(id="gpu-box-01", company="c1", device="dev_1", capacity={"cpus": 32})
        self.assertEqual(worker.state, "active")
        self.assertFalse(worker.exclusive_task)

    def test_reservation_state_values(self):
        self.assertEqual(
            {ReservationState.offered, ReservationState.claimed, ReservationState.released},
            {"offered", "claimed", "released"},
        )

    def test_reservation_is_indexed_by_task_and_worker(self):
        indexes = Reservation._meta["indexes"]
        flat = [str(i) for i in indexes]
        self.assertTrue(any("task" in i for i in flat))
        self.assertTrue(any("worker" in i for i in flat))

    def test_event_requires_kind_and_company(self):
        event = SchedulerEvent(company="c1", kind="placed", task="t1", time=datetime(2026, 1, 1))
        self.assertEqual(event.kind, "placed")
        event.validate()
        with self.assertRaises(ValidationError):
            SchedulerEvent(kind="placed").validate()
