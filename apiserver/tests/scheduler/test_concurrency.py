"""Placement passes run at the same time (several API processes, or threads) must not place a task twice
or hand the same CPU to two jobs."""

import threading
from collections import Counter

from apiserver import database as db
from apiserver.bll.scheduler import service
from apiserver.database.model.device import Device
from apiserver.database.model.scheduler import Reservation, SchedulerWorker
from apiserver.tests.automated import TestService
from apiserver.tests.scheduler.agent import AgentDevice
from apiserver.tests.scheduler.test_protocol import WHOLE_MACHINE, agent_call

SMALL = {"cpus": 1, "ram_gb": 1, "gpus": {"count": 0, "min_memory_gb": 0, "model": None}, "disk_gb": 1}


class SchedulerConcurrencyTest(TestService):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        super().setUp()
        self.agent = AgentDevice()
        self.worker = f"test-worker-conc-{self.agent.device[-8:]}"
        self.company = Device.objects(id=self.agent.device).first().company
        agent_call(self.agent, "scheduler.register_worker", {
            "worker": self.worker,
            "capacity": {"cpus": 16, "ram_gb": 64, "disk_gb": 400, "gpus": []},
            "reserved": {"cpus": 1, "ram_gb": 2, "disk_gb": 10},
            "can_enforce": {"docker": True},
        })

    def tearDown(self):
        super().tearDown()
        Reservation.objects(worker=self.worker).delete()
        SchedulerWorker.objects(id=self.worker).delete()

    def test_parallel_passes_place_each_task_once_and_never_share_a_cpu(self):
        ids = []
        for _ in range(12):
            task = self.create_temp("tasks", name="conc", type="training")
            self.api.send("tasks.enqueue", {"task": task, "requirements": SMALL})
            ids.append(task)
        threads = [threading.Thread(target=service.placement_pass, args=(self.company,)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # other workers of the company may take some of the tasks; the rules must hold across all of them
        live = list(Reservation.objects(task__in=ids, state__in=["offered", "claimed"]))
        tasks = Counter(r.task for r in live)
        self.assertTrue(all(count == 1 for count in tasks.values()), "a task was placed twice")
        for worker in {r.worker for r in live}:
            cpus = Counter(c for r in live if r.worker == worker for c in r.cpus)
            self.assertTrue(all(count == 1 for count in cpus.values()), f"a CPU was handed out twice on {worker}")
        self.assertGreater(len(live), 0)


class SchedulerLockTest(TestService):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def test_a_pass_that_cannot_get_the_lock_places_nothing(self):
        from unittest import mock
        with mock.patch.object(service, "_company_lock") as lock:
            lock.return_value.__enter__.return_value = False
            self.assertEqual(service.placement_pass("no-such-company"), 0)
