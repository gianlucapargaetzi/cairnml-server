"""The agent-facing protocol over HTTP: register, sync, offers, claim, release, and who may call."""

import requests

from apiserver import database as db
from apiserver.database.model.scheduler import Reservation, SchedulerWorker
from apiserver.tests.automated import TestService
from apiserver.tests.scheduler.agent import AgentDevice

BASE = "http://localhost:8008/v2.36"
WHOLE_MACHINE = {"cpus": 4, "ram_gb": 8, "gpus": {"count": 0, "min_memory_gb": 0, "model": None}, "disk_gb": 10}


def agent_call(agent, endpoint, body, expect_ok=True):
    res = requests.post(f"{BASE}/{endpoint}", json=body, headers=agent.headers(), timeout=20)
    payload = res.json()
    if expect_ok:
        assert res.status_code == 200, payload
        return payload["data"]
    return payload


class SchedulerProtocolTest(TestService):
    @classmethod
    def setUpClass(cls):
        db.db.initialize()
        # workers left by earlier runs would take offers meant for this run's workers
        for leftover in SchedulerWorker.objects(id__startswith="test-worker-"):
            Reservation.objects(worker=leftover.id).delete()
            leftover.delete()

    def setUp(self):
        super().setUp()
        self.agent = AgentDevice()
        self.worker = f"test-worker-{self.agent.device[-8:]}"
        self.register()

    def tearDown(self):
        super().tearDown()
        Reservation.objects(worker=self.worker).delete()
        SchedulerWorker.objects(id=self.worker).delete()

    def register(self, agent=None):
        agent = agent or self.agent
        return agent_call(agent, "scheduler.register_worker", {
            "worker": self.worker,
            "capacity": {"cpus": 8, "ram_gb": 32, "disk_gb": 200, "gpus": []},
            "reserved": {"cpus": 1, "ram_gb": 2, "disk_gb": 10},
            "can_enforce": {"docker": True},
        })

    def task(self, requirements=WHOLE_MACHINE, priority=50):
        task = self.create_temp("tasks", name="protocol-test", type="training")
        self.api.send("tasks.enqueue", {"task": task, "requirements": requirements, "priority": priority})
        return task

    def offers(self):
        return agent_call(self.agent, "scheduler.sync", {"worker": self.worker, "jobs": [], "usage": {}})["offers"]

    def test_registering_then_syncing_offers_a_fitting_task(self):
        task = self.task()
        offered = [o["task"] for o in self.offers()]
        self.assertIn(task, offered)

    def test_claim_starts_the_task_and_remembers_the_device(self):
        task = self.task()
        self.offers()
        agent_call(self.agent, "scheduler.claim", {"worker": self.worker, "task": task})
        _, data = self.api.send("tasks.get_by_id", {"task": task})
        self.assertEqual(data["task"]["status"], "in_progress")
        self.assertEqual(data["task"]["last_device"], self.agent.device)

    def test_a_second_claim_of_the_same_offer_is_refused(self):
        task = self.task()
        self.offers()
        agent_call(self.agent, "scheduler.claim", {"worker": self.worker, "task": task})
        again = agent_call(self.agent, "scheduler.claim", {"worker": self.worker, "task": task}, expect_ok=False)
        self.assertNotEqual(again["meta"]["result_code"], 200)

    def test_another_device_cannot_sync_or_claim_for_this_worker(self):
        other = AgentDevice()
        task = self.task()
        self.offers()
        refused = agent_call(other, "scheduler.sync", {"worker": self.worker}, expect_ok=False)
        self.assertNotEqual(refused["meta"]["result_code"], 200)
        refused = agent_call(other, "scheduler.claim", {"worker": self.worker, "task": task}, expect_ok=False)
        self.assertNotEqual(refused["meta"]["result_code"], 200)

    def test_a_user_token_cannot_use_the_agent_calls(self):
        res = requests.post(f"{BASE}/scheduler.sync", json={"worker": self.worker},
                            headers={"Authorization": self.api.session_token and f"Bearer {self.api.session_token}"},
                            timeout=20)
        self.assertNotEqual(res.status_code, 200)

    def test_a_completed_job_completes_the_task(self):
        task = self.task()
        self.offers()
        agent_call(self.agent, "scheduler.claim", {"worker": self.worker, "task": task})
        agent_call(self.agent, "scheduler.release", {"worker": self.worker, "task": task, "outcome": "completed"})
        _, data = self.api.send("tasks.get_by_id", {"task": task})
        self.assertEqual(data["task"]["status"], "completed")

    def test_an_out_of_memory_job_fails_with_its_reason_and_numbers(self):
        task = self.task()
        self.offers()
        agent_call(self.agent, "scheduler.claim", {"worker": self.worker, "task": task})
        agent_call(self.agent, "scheduler.release", {
            "worker": self.worker, "task": task, "outcome": "failed",
            "failure": {"reason": "oom", "detail": "Failure due to bad compute: out of memory (limit 12 GB)",
                        "reserved": {"ram_gb": 12}, "peak": {"ram_gb": 12.0}},
        })
        _, data = self.api.send("tasks.get_by_id", {"task": task})
        self.assertEqual(data["task"]["status"], "failed")
        self.assertEqual(data["task"]["failure"]["reason"], "oom")
        self.assertEqual(data["task"]["failure"]["reserved"], {"ram_gb": 12})

    def test_a_job_the_server_no_longer_expects_is_listed_to_stop(self):
        task = self.task()
        self.offers()
        result = agent_call(self.agent, "scheduler.sync", {
            "worker": self.worker, "jobs": [{"task": task, "usage": {}}],
        })
        self.assertIn(task, result["stop"])

    def test_the_task_is_no_longer_queued_refuses_the_claim(self):
        task = self.task()
        self.offers()
        self.api.send("tasks.stop", {"task": task, "force": True})
        refused = agent_call(self.agent, "scheduler.claim", {"worker": self.worker, "task": task}, expect_ok=False)
        self.assertNotEqual(refused["meta"]["result_code"], 200)
