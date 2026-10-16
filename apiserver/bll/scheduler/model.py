"""Rules for what a task may ask for, and the priority it may carry.

A task either names all of its compute requirements or none. A partial set is refused, so the
scheduler never has to guess. A task with none runs alone on a worker.
"""

from typing import Optional

from pyhocon import ConfigTree

from apiserver.apierrors import errors
from apiserver.config_repo import config

MAIN_FIELDS = ("cpus", "ram_gb", "gpus", "disk_gb")
OPTIONAL_FIELDS = ("max_runtime_min",)
MIN_PRIORITY = 0
MAX_PRIORITY = 100


class RequirementsError(errors.bad_request.ValidationError):
    pass


def _settings() -> ConfigTree:
    return config.get("services.scheduler", ConfigTree())


def _bound(name: str):
    return _settings().get(f"max_requirements.{name}")


def _check_bound(name: str, value):
    limit = _bound(name)
    if limit is not None and value > limit:
        raise RequirementsError(f"{name} {value} is above the server limit {limit}")


def validate_requirements(requirements: Optional[dict]) -> Optional[dict]:
    """Return the normalised requirements, or None when the task names none."""
    if not requirements:
        return None

    given = [f for f in MAIN_FIELDS if requirements.get(f) is not None]
    if not given:
        return None
    missing = [f for f in MAIN_FIELDS if requirements.get(f) is None]
    if missing:
        raise RequirementsError(
            "requirements must name all of cpus, ram_gb, gpus and disk_gb, or none; missing: "
            + ", ".join(missing)
        )

    cpus = requirements["cpus"]
    ram_gb = requirements["ram_gb"]
    disk_gb = requirements["disk_gb"]
    gpus = requirements["gpus"]
    if not isinstance(cpus, int) or isinstance(cpus, bool) or cpus < 1:
        raise RequirementsError("cpus must be an integer of at least 1")
    if not isinstance(ram_gb, (int, float)) or isinstance(ram_gb, bool) or ram_gb <= 0:
        raise RequirementsError("ram_gb must be a number above 0")
    if not isinstance(disk_gb, (int, float)) or isinstance(disk_gb, bool) or disk_gb < 0:
        raise RequirementsError("disk_gb must be a number of at least 0")
    if not isinstance(gpus, dict) or not isinstance(gpus.get("count"), int) or gpus["count"] < 0:
        raise RequirementsError("gpus must be an object with count, an integer of at least 0")

    _check_bound("cpus", cpus)
    _check_bound("ram_gb", ram_gb)
    _check_bound("gpus", gpus["count"])
    _check_bound("disk_gb", disk_gb)

    result = {
        "cpus": cpus,
        "ram_gb": float(ram_gb),
        "gpus": {
            "count": gpus["count"],
            "min_memory_gb": float(gpus.get("min_memory_gb") or 0),
            "model": gpus.get("model") or None,
        },
        "disk_gb": float(disk_gb),
    }
    max_runtime = requirements.get("max_runtime_min")
    if max_runtime is not None:
        if not isinstance(max_runtime, int) or isinstance(max_runtime, bool) or max_runtime < 1:
            raise RequirementsError("max_runtime_min must be an integer of at least 1")
        _check_bound("max_runtime_min", max_runtime)
        result["max_runtime_min"] = max_runtime
    return result


def validate_priority(priority) -> int:
    if priority is None:
        return int(_settings().get("default_priority", 50))
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise RequirementsError("priority must be an integer")
    if not MIN_PRIORITY <= priority <= MAX_PRIORITY:
        raise RequirementsError(f"priority must be between {MIN_PRIORITY} and {MAX_PRIORITY}")
    return priority
