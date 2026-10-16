from apiserver.tests.automated import TestService


class TestEnqueueRequirements(TestService):
    def _task(self):
        return self.create_temp("tasks", name="sched-test", type="training")

    def test_partial_requirements_are_refused_with_the_missing_fields(self):
        task = self._task()
        with self.assertRaises(Exception) as ctx:
            self.api.send("tasks.enqueue", {"task": task, "requirements": {"cpus": 4, "ram_gb": 12}})
        self.assertIn("missing", str(ctx.exception))
        self.assertIn("gpus", str(ctx.exception))

    def test_full_requirements_and_priority_are_stored(self):
        task = self._task()
        requirements = {
            "cpus": 4,
            "ram_gb": 12,
            "gpus": {"count": 1, "min_memory_gb": 24},
            "disk_gb": 50,
            "max_runtime_min": 720,
        }
        self.api.send("tasks.enqueue", {"task": task, "requirements": requirements, "priority": 80})
        _, data = self.api.send("tasks.get_by_id", {"task": task})
        stored = data["task"]
        self.assertEqual(stored["priority"], 80)
        self.assertEqual(stored["requirements"]["cpus"], 4)
        self.assertEqual(stored["requirements"]["gpus"]["min_memory_gb"], 24)
        self.assertEqual(stored["requirements"]["max_runtime_min"], 720)

    def test_no_requirements_means_whole_machine_and_default_priority(self):
        task = self._task()
        self.api.send("tasks.enqueue", {"task": task})
        _, data = self.api.send("tasks.get_by_id", {"task": task})
        self.assertFalse(data["task"].get("requirements"))
        self.assertEqual(data["task"]["priority"], 50)

    def test_priority_out_of_range_is_refused(self):
        task = self._task()
        with self.assertRaises(Exception):
            self.api.send("tasks.enqueue", {"task": task, "priority": 101})
