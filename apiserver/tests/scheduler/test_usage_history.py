"""Usage history: each sync stores the machine's and the jobs' usage in Elasticsearch, and it can be read back
by worker and by task. A drained worker takes no new offers."""

from datetime import datetime, timedelta

from apiserver import database as db
from apiserver.bll.scheduler import usage_history
from apiserver.database.model.device import Device
from apiserver.database.model.scheduler import Reservation, SchedulerWorker
from apiserver.tests.automated import TestService
from apiserver.tests.scheduler.agent import AgentDevice
from apiserver.tests.scheduler.test_protocol import WHOLE_MACHINE, agent_call


class UsageHistoryTest(TestService):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()

    def setUp(self):
        super().setUp()
        self.agent = AgentDevice()
        self.worker = f"test-worker-hist-{self.agent.device[-8:]}"
        self.company = Device.objects(id=self.agent.device).first().company
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
        usage_history.delete_worker(self.company, self.worker)

    def test_a_sync_is_stored_and_read_back_by_worker_and_by_task(self):
        task = self.create_temp("tasks", name="hist", type="training")
        self.api.send("tasks.enqueue", {"task": task, "requirements": WHOLE_MACHINE})
        agent_call(self.agent, "scheduler.sync", {"worker": self.worker, "jobs": [], "usage": {"cpu_percent": 12.5,
                                                                                              "ram_used_gb": 4.0}})
        agent_call(self.agent, "scheduler.sync", {"worker": self.worker, "jobs": [], "usage": {"cpu_percent": 20.0,
                                                                                              "ram_used_gb": 5.0}})
        now = datetime.utcnow()
        machine = usage_history.query(self.company, worker=self.worker,
                                      start=now - timedelta(minutes=5), end=now + timedelta(minutes=1))
        self.assertGreaterEqual(len(machine), 2)
        self.assertEqual(machine[0]["worker"], self.worker)
        self.assertIn("cpu_percent", machine[0])

    def test_job_usage_is_read_back_by_task(self):
        task = self.create_temp("tasks", name="hist-job", type="training")
        self.api.send("tasks.enqueue", {"task": task, "requirements": WHOLE_MACHINE})
        agent_call(self.agent, "scheduler.sync", {"worker": self.worker, "jobs": [], "usage": {}})
        agent_call(self.agent, "scheduler.claim", {"worker": self.worker, "task": task})
        agent_call(self.agent, "scheduler.sync", {"worker": self.worker,
                                                  "jobs": [{"task": task, "usage": {"ram_gb": 2.5}}], "usage": {}})
        now = datetime.utcnow()
        jobs = usage_history.query(self.company, task=task, start=now - timedelta(minutes=5), end=now + timedelta(minutes=1))
        self.assertTrue(any(p.get("ram_gb") == 2.5 for p in jobs))

    def test_a_drained_worker_takes_no_new_offers(self):
        agent_call(self.agent, "scheduler.drain_worker", {"worker": self.worker, "drain": True}, expect_ok=False)
        task = self.create_temp("tasks", name="drain", type="training")
        self.api.send("tasks.enqueue", {"task": task, "requirements": WHOLE_MACHINE})
        SchedulerWorker.objects(id=self.worker).update(state="draining")
        offers = agent_call(self.agent, "scheduler.sync", {"worker": self.worker, "jobs": [], "usage": {}})["offers"]
        self.assertEqual(offers, [])
