from datetime import datetime, timedelta
from unittest import TestCase

from apiserver.bll.scheduler.placement import (
    WorkerView,
    PendingTask,
    plan,
    reason_for,
)

T0 = datetime(2026, 1, 1, 12, 0, 0)


def gpu(index, memory_gb=48, model="L40S"):
    return {"index": index, "memory_gb": memory_gb, "model": model}


def worker(name, cpus=32, ram=32, disk=800, gpus=(), reserved_cpus=2, reserved_ram=4, reserved_disk=100,
           docker=True, state="active", busy=False, **kwargs):
    return WorkerView(
        id=name,
        state=state,
        cpus=cpus,
        ram_gb=ram,
        disk_gb=disk,
        gpus=list(gpus),
        reserved_cpus=reserved_cpus,
        reserved_ram_gb=reserved_ram,
        reserved_disk_gb=reserved_disk,
        can_enforce={"docker": docker, "gpu_device_limit": True, "cpuset": True},
        exclusive_busy=busy,
        **kwargs,
    )


def task(tid, cpus=4, ram=12, gpus=0, gpu_mem=0, model=None, disk=50, priority=50, minutes=0, runtime=None):
    req = None
    if cpus is not None:
        req = {
            "cpus": cpus,
            "ram_gb": ram,
            "gpus": {"count": gpus, "min_memory_gb": gpu_mem, "model": model},
            "disk_gb": disk,
        }
        if runtime:
            req["max_runtime_min"] = runtime
    return PendingTask(id=tid, priority=priority, submitted=T0 + timedelta(minutes=minutes), requirements=req)


class TestPlacementRules(TestCase):
    def test_a_task_that_fits_is_placed_with_concrete_cpus(self):
        result = plan([task("a")], [worker("w1")])
        self.assertEqual(len(result.placements), 1)
        p = result.placements[0]
        self.assertEqual((p.task, p.worker), ("a", "w1"))
        self.assertEqual(p.cpus, [2, 3, 4, 5])  # the first 2 cpus are kept for the host
        self.assertEqual(p.gpus, [])

    def test_host_reserve_is_never_handed_out(self):
        result = plan([task("a", cpus=30, ram=28, disk=700)], [worker("w1")])
        self.assertEqual(len(result.placements), 1)
        self.assertNotIn(0, result.placements[0].cpus)
        self.assertNotIn(1, result.placements[0].cpus)

    def test_a_task_bigger_than_the_schedulable_part_is_not_placed(self):
        result = plan([task("a", cpus=31)], [worker("w1")])  # 32 cpus minus 2 reserved = 30
        self.assertEqual(result.placements, [])
        self.assertIn("no worker", result.reasons["a"])

    def test_best_fit_leaves_the_large_machine_free(self):
        small = worker("small", cpus=8, ram=16, disk=200)
        large = worker("large", cpus=64, ram=256, disk=2000)
        result = plan([task("a", cpus=4, ram=8)], [large, small])
        self.assertEqual(result.placements[0].worker, "small")

    def test_gpu_count_and_memory_must_match(self):
        w = worker("w1", gpus=[gpu(0, 24), gpu(1, 48)])
        result = plan([task("a", cpus=2, ram=4, gpus=1, gpu_mem=40)], [w])
        self.assertEqual(result.placements[0].gpus, [1])

    def test_gpu_model_must_match_when_given(self):
        w = worker("w1", gpus=[gpu(0, 48, "T4"), gpu(1, 48, "L40S")])
        result = plan([task("a", cpus=2, ram=4, gpus=1, model="L40S")], [w])
        self.assertEqual(result.placements[0].gpus, [1])

    def test_no_matching_gpu_gives_a_reason_that_names_the_memory(self):
        w = worker("w1", gpus=[gpu(0, 24)])
        result = plan([task("a", cpus=2, ram=4, gpus=1, gpu_mem=40)], [w])
        self.assertEqual(result.placements, [])
        self.assertIn("GPU", result.reasons["a"])
        self.assertIn("40", result.reasons["a"])

    def test_priority_order_then_submission_order(self):
        w = worker("w1", cpus=6, ram=40, disk=400, reserved_cpus=0, reserved_ram=0, reserved_disk=0)
        low_first = task("low", cpus=4, priority=10, minutes=0)
        high_late = task("high", cpus=4, priority=90, minutes=5)
        result = plan([low_first, high_late], [w])
        self.assertEqual([p.task for p in result.placements], ["high"])

    def test_ties_go_to_the_earlier_submission(self):
        w = worker("w1", cpus=6, ram=40, disk=400, reserved_cpus=0, reserved_ram=0, reserved_disk=0)
        later = task("later", cpus=4, minutes=5)
        earlier = task("earlier", cpus=4, minutes=0)
        result = plan([later, earlier], [w])
        self.assertEqual([p.task for p in result.placements], ["earlier"])

    def test_backfill_places_a_smaller_task_past_a_blocked_one(self):
        w = worker("w1", cpus=8, ram=40, disk=400, reserved_cpus=0, reserved_ram=0, reserved_disk=0)
        big = task("big", cpus=16, priority=90)
        small = task("small", cpus=2, priority=10)
        result = plan([big, small], [w])
        self.assertEqual([p.task for p in result.placements], ["small"])
        self.assertIn("big", result.reasons)

    def test_a_whole_machine_task_waits_for_an_empty_worker(self):
        busy = worker("w1", busy=False)
        result = plan([task("whole", cpus=None)], [busy])
        self.assertEqual(result.placements[0].exclusive, True)
        self.assertEqual(result.placements[0].cpus, list(range(2, 32)))

    def test_an_exclusive_job_is_not_placed_on_a_worker_with_other_jobs(self):
        w = worker("w1", running_reservations=1)
        result = plan([task("whole", cpus=None)], [w])
        self.assertEqual(result.placements, [])
        self.assertIn("whole machine", result.reasons["whole"])

    def test_an_exclusive_worker_takes_nothing_else(self):
        w = worker("w1", busy=True)
        result = plan([task("small", cpus=1, ram=1, disk=1)], [w])
        self.assertEqual(result.placements, [])

    def test_a_worker_that_cannot_enforce_limits_is_used_only_for_exclusive_jobs(self):
        w = worker("w1", docker=False)
        result = plan([task("small", cpus=1, ram=1, disk=1), task("whole", cpus=None, priority=10)], [w])
        self.assertEqual([p.task for p in result.placements], ["whole"])

    def test_a_draining_or_lost_worker_gets_nothing(self):
        for state in ("draining", "lost"):
            result = plan([task("a", cpus=1, ram=1, disk=1)], [worker("w1", state=state)])
            self.assertEqual(result.placements, [], state)

    def test_reason_for_a_task_that_waits_only_because_others_run(self):
        w = worker("w1", cpus=8, ram=40, disk=400, reserved_cpus=0, reserved_ram=0, reserved_disk=0,
                   allocated_cpus={0, 1, 2, 3, 4, 5, 6, 7})
        self.assertIn("waiting", reason_for(task("a", cpus=2), [w]))
