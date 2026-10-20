"""Offers expire, lost workers are retried once and then failed. Uses simulated time."""

from datetime import datetime, timedelta

from apiserver import database as db
from apiserver.bll.scheduler import service
from apiserver.database.model.device import Device
from apiserver.database.model.scheduler import Reservation, SchedulerWorker
from apiserver.database.model.task.task import Task
from apiserver.tests.automated import TestService
from apiserver.tests.scheduler.agent import AgentDevice
from apiserver.tests.scheduler.test_protocol import WHOLE_MACHINE, agent_call

# the clock of the tests starts at the real time, so the registration (real time) is consistent with it
T0 = datetime.utcnow()


class SchedulerLifecycleTest(TestService):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        super().setUp()
        self.agent = AgentDevice()
        self.worker = f"test-worker-life-{self.agent.device[-8:]}"
        self.company = Device.objects(id=self.agent.device).first().company
        self.user = Device.objects(id=self.agent.device).first().user
        agent_call(self.agent, "scheduler.register_worker", {
            "worker": self.worker,
            "capacity": {"cpus": 8, "ram_gb": 32, "disk_gb": 200, "gpus": []},
            "reserved": {"cpus": 1, "ram_gb": 2, "disk_gb": 10},
            "can_enforce": {"docker": True},
        })

    def tearDown(self):
        super().tearDown()
        Reservation.objects(worker=self.worker).delete()
        SchedulerWorker.objects(id=self.worker).delete()

    def new_queued_task(self):
        task = self.create_temp("tasks", name="lifecycle", type="training")
        self.api.send("tasks.enqueue", {"task": task, "requirements": WHOLE_MACHINE})
        return task

    def test_an_unclaimed_offer_expires_and_the_task_is_pending_again(self):
        task = self.new_queued_task()
        r = Reservation.objects(task=task, state="offered").first()
        self.assertIsNotNone(r)
        service.expire_offers(self.company, r.offered_at + timedelta(seconds=120))
        self.assertEqual(Reservation.objects(id=r.id).first().state, "released")
        self.assertEqual(Task.objects(id=task).first().status, "queued")

    def test_a_lost_worker_retries_its_job_once(self):
        task = self.new_queued_task()
        service.claim(self.company, self.agent.device, self.worker, task, now=T0)
        service.mark_lost_workers(self.company, T0 + timedelta(minutes=5))
        self.assertEqual(SchedulerWorker.objects(id=self.worker).first().state, "lost")
        self.assertEqual(Task.objects(id=task).first().status, "queued")

    def test_the_sweep_marks_a_stopped_worker_lost_without_a_placement_pass(self):
        """The sweep timer finds the stopped worker by itself: no task is queued, so no placement pass runs."""
        service.sweep(self.company, now=T0 + timedelta(minutes=5))
        self.assertEqual(SchedulerWorker.objects(id=self.worker).first().state, "lost")

    def test_a_job_lost_twice_fails_with_the_worker_lost_reason(self):
        task = self.new_queued_task()
        service.claim(self.company, self.agent.device, self.worker, task, now=T0)
        service.mark_lost_workers(self.company, T0 + timedelta(minutes=5))
        # the worker comes back, takes the retry, and is lost again
        SchedulerWorker.objects(id=self.worker).update(state="active", last_sync=T0 + timedelta(minutes=5))
        service.placement_pass(self.company, now=T0 + timedelta(minutes=5))
        service.claim(self.company, self.agent.device, self.worker, task, now=T0 + timedelta(minutes=5))
        service.mark_lost_workers(self.company, T0 + timedelta(minutes=10))
        stored = Task.objects(id=task).first()
        self.assertEqual(stored.status, "failed")
        self.assertEqual(stored.failure["reason"], "worker_lost")

    def test_a_returning_worker_is_active_again(self):
        service.mark_lost_workers(self.company, T0 + timedelta(minutes=5))
        self.assertEqual(SchedulerWorker.objects(id=self.worker).first().state, "lost")
        service.sync(self.company, self.agent.device, self.worker, jobs=[], usage={}, now=T0 + timedelta(minutes=6))
        self.assertEqual(SchedulerWorker.objects(id=self.worker).first().state, "active")
