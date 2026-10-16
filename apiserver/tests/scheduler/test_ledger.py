import random
from datetime import datetime, timedelta
from unittest import TestCase

from apiserver.bll.scheduler.ledger import views_for
from apiserver.bll.scheduler.placement import PendingTask, plan

T0 = datetime(2026, 1, 1)


def worker_doc(name, cpus, ram, disk, gpus, reserved_cpus=2, reserved_ram=4, reserved_disk=50):
    return {
        "id": name,
        "state": "active",
        "capacity": {"cpus": cpus, "ram_gb": ram, "disk_gb": disk,
                     "gpus": [{"index": i, "model": "X", "memory_gb": 24} for i in range(gpus)]},
        "reserved": {"cpus": reserved_cpus, "ram_gb": reserved_ram, "disk_gb": reserved_disk},
        "can_enforce": {"docker": True},
    }


def as_reservation(placement, worker_name, state="offered"):
    return {
        "task": placement.task,
        "worker": worker_name,
        "state": state,
        "cpus": placement.cpus,
        "gpus": placement.gpus,
        "ram_gb": placement.ram_gb,
        "disk_gb": placement.disk_gb,
        "exclusive": placement.exclusive,
    }


class TestLedger(TestCase):
    def test_released_and_released_reservations_do_not_count(self):
        workers = [worker_doc("w1", 8, 16, 100, 0)]
        reservations = [{"worker": "w1", "state": "released", "cpus": [2, 3], "ram_gb": 4, "disk_gb": 1}]
        view = views_for(workers, reservations)[0]
        self.assertEqual(view.allocated_cpus, set())
        self.assertEqual(view.running_reservations, 0)

    def test_offered_and_claimed_both_count(self):
        workers = [worker_doc("w1", 8, 16, 100, 0)]
        reservations = [
            {"worker": "w1", "state": "offered", "cpus": [2], "ram_gb": 2, "disk_gb": 1},
            {"worker": "w1", "state": "claimed", "cpus": [3], "ram_gb": 2, "disk_gb": 1},
        ]
        view = views_for(workers, reservations)[0]
        self.assertEqual(view.allocated_cpus, {2, 3})
        self.assertEqual(view.allocated_ram_gb, 4)

    def test_an_exclusive_reservation_marks_the_worker_busy(self):
        workers = [worker_doc("w1", 8, 16, 100, 0)]
        reservations = [{"worker": "w1", "state": "claimed", "cpus": [2, 3, 4, 5, 6, 7],
                         "ram_gb": 12, "disk_gb": 46, "exclusive": True}]
        self.assertTrue(views_for(workers, reservations)[0].exclusive_busy)

    def test_randomised_place_and_release_never_overcommits(self):
        """Many random rounds of placing and releasing. After each round, no CPU, GPU, RAM or disk is
        counted twice, and nothing is allocated beyond what the worker can give."""
        rng = random.Random(7)
        workers = [
            worker_doc("w1", 32, 64, 800, 2),
            worker_doc("w2", 16, 32, 400, 1),
            worker_doc("w3", 8, 16, 200, 0),
        ]
        live = []  # (worker name, reservation dict)
        placed_total = 0
        for round_no in range(300):
            if live and rng.random() < 0.4:
                live.pop(rng.randrange(len(live)))
            pending = []
            for n in range(rng.randint(1, 4)):
                if rng.random() < 0.15:
                    req = None
                else:
                    req = {"cpus": rng.randint(1, 12), "ram_gb": rng.uniform(1, 20),
                           "gpus": {"count": rng.randint(0, 2), "min_memory_gb": 0, "model": None},
                           "disk_gb": rng.uniform(0, 50)}
                pending.append(PendingTask(id=f"t{round_no}-{n}", priority=rng.randint(0, 100),
                                           submitted=T0 + timedelta(minutes=round_no), requirements=req))
            views = views_for(workers, [r for _, r in live])
            result = plan(pending, views)
            for p in result.placements:
                live.append((p.worker, as_reservation(p, p.worker)))
                placed_total += 1
            self._assert_consistent(workers, live)
        self.assertGreater(placed_total, 100, "the randomised run placed too little to mean anything")

    def _assert_consistent(self, workers, live):
        for doc in workers:
            mine = [r for w, r in live if w == doc["id"]]
            cpu_ids = [c for r in mine for c in r["cpus"]]
            gpu_ids = [g for r in mine for g in r["gpus"]]
            self.assertEqual(len(cpu_ids), len(set(cpu_ids)), "cpu counted twice")
            self.assertEqual(len(gpu_ids), len(set(gpu_ids)), "gpu counted twice")
            self.assertTrue(all(c >= doc["reserved"]["cpus"] for c in cpu_ids), "host reserve handed out")
            self.assertLessEqual(sum(r["ram_gb"] for r in mine),
                                 doc["capacity"]["ram_gb"] - doc["reserved"]["ram_gb"] + 1e-6)
            self.assertLessEqual(sum(r["disk_gb"] for r in mine),
                                 doc["capacity"]["disk_gb"] - doc["reserved"]["disk_gb"] + 1e-6)
            if any(r["exclusive"] for r in mine):
                self.assertEqual(len(mine), 1, "an exclusive job shares its worker")
