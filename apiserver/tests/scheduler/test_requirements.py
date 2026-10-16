from unittest import TestCase

from apiserver.bll.scheduler.model import (
    RequirementsError,
    validate_requirements,
    validate_priority,
)


def full(**overrides):
    req = {
        "cpus": 4,
        "ram_gb": 12,
        "gpus": {"count": 1, "min_memory_gb": 24, "model": None},
        "disk_gb": 50,
    }
    req.update(overrides)
    return req


class TestRequirements(TestCase):
    def test_all_main_fields_are_accepted(self):
        self.assertEqual(validate_requirements(full())["cpus"], 4)

    def test_no_requirements_is_accepted_as_none(self):
        self.assertIsNone(validate_requirements(None))
        self.assertIsNone(validate_requirements({}))

    def test_partial_set_is_refused_and_names_the_missing_fields(self):
        with self.assertRaises(RequirementsError) as ctx:
            validate_requirements({"cpus": 4, "ram_gb": 12})
        self.assertIn("gpus", str(ctx.exception))
        self.assertIn("disk_gb", str(ctx.exception))
        self.assertNotIn("cpus", str(ctx.exception).split("missing")[-1])

    def test_gpu_count_zero_is_a_valid_gpu_answer(self):
        req = validate_requirements(full(gpus={"count": 0, "min_memory_gb": 0, "model": None}))
        self.assertEqual(req["gpus"]["count"], 0)

    def test_optional_max_runtime_is_kept(self):
        req = validate_requirements(full(max_runtime_min=720))
        self.assertEqual(req["max_runtime_min"], 720)

    def test_zero_cpus_refused(self):
        with self.assertRaises(RequirementsError):
            validate_requirements(full(cpus=0))

    def test_negative_ram_refused(self):
        with self.assertRaises(RequirementsError):
            validate_requirements(full(ram_gb=-1))

    def test_above_the_configured_bound_refused(self):
        with self.assertRaises(RequirementsError) as ctx:
            validate_requirements(full(cpus=10_000))
        self.assertIn("cpus", str(ctx.exception))

    def test_priority_defaults_and_bounds(self):
        self.assertEqual(validate_priority(None), 50)
        self.assertEqual(validate_priority(0), 0)
        self.assertEqual(validate_priority(100), 100)
        with self.assertRaises(RequirementsError):
            validate_priority(101)
        with self.assertRaises(RequirementsError):
            validate_priority(-1)
        with self.assertRaises(RequirementsError):
            validate_priority("high")
