"""Simulated time: many tasks on several workers. The placement must never overcommit, and every task
must run eventually (no task waits forever behind others)."""

import random
from datetime import datetime, timedelta
from unittest import TestCase

from apiserver.bll.scheduler.ledger import views_for
from apiserver.bll.scheduler.placement import PendingTask, plan

START = datetime(2026, 1, 1)


def worker_doc(name, cpus, ram, gpus):
    return {
        "id": name,
        "state": "active",
        "capacity": {"cpus": cpus, "ram_gb": ram, "disk_gb": 500,
                     "gpus": [{"index": i, "model": "L40S", "memory_gb": 48} for i in range(gpus)]},
        "reserved": {"cpus": 1, "ram_gb": 2, "disk_gb": 20},
        "can_enforce": {"docker": True},
    }


class TestSimulator(TestCase):
    def test_three_hundred_tasks_on_ten_workers(self):
        rng = random.Random(42)
        workers = [worker_doc(f"w{i}", rng.choice([8, 16, 32, 64]), rng.choice([32, 64, 128]),
                           rng.choice([0, 1, 2])) for i in range(10)]

        tasks = {}
        for n in range(300):
            exclusive = rng.random() < 0.05
            req = None if exclusive else {
                "cpus": rng.randint(1, 8), "ram_gb": float(rng.randint(1, 24)),
                "gpus": {"count": rng.choice([0, 0, 0, 1]), "min_memory_gb": 0, "model": None},
                "disk_gb": float(rng.randint(0, 20)),
            }
            tasks[f"t{n}"] = {
                "pending": PendingTask(id=f"t{n}", priority=rng.randint(0, 100),
                                       submitted=START + timedelta(seconds=n), requirements=req),
                "minutes": rng.randint(1, 30),
                "started": None,
            }

        running = {}  # task -> (worker, placement, ends)
        now = START
        waiting_max = timedelta(0)
        for _ in range(2000):
            for tid, info in list(running.items()):
                if now >= info["ends"]:
                    running.pop(tid)
            pending = [t["pending"] for t in tasks.values() if t["started"] is None]
            reservations = [
                {"worker": w, "state": "claimed", "cpus": p.cpus, "gpus": p.gpus,
                 "ram_gb": p.ram_gb, "disk_gb": p.disk_gb, "exclusive": p.exclusive}
                for w, p in ((info["worker"], info["placement"]) for info in running.values())
            ]
            result = plan(pending, views_for(workers, reservations))
            for p in result.placements:
                tasks[p.task]["started"] = now
                waiting_max = max(waiting_max, now - START)
                running[p.task] = {"worker": p.worker, "placement": p, "ends": now + timedelta(minutes=tasks[p.task]["minutes"])}
            self._assert_no_overcommit(workers, running)
            if all(t["started"] is not None for t in tasks.values()):
                break
            now += timedelta(minutes=1)

        never_started = [tid for tid, t in tasks.items() if t["started"] is None]
        self.assertEqual(never_started, [], "tasks never started")
        self.assertLess(waiting_max, timedelta(hours=10), "a task waited far too long")

    def _assert_no_overcommit(self, workers, running):
        for doc in workers:
            mine = [info["placement"] for info in running.values() if info["worker"] == doc["id"]]
            cpus = [c for p in mine for c in p.cpus]
            gpus = [g for p in mine for g in p.gpus]
            assert len(cpus) == len(set(cpus)), "cpu overcommit"
            assert len(gpus) == len(set(gpus)), "gpu overcommit"
            assert sum(p.ram_gb for p in mine) <= doc["capacity"]["ram_gb"] - doc["reserved"]["ram_gb"] + 1e-6
            assert all(c >= doc["reserved"]["cpus"] for c in cpus), "host reserve handed out"
