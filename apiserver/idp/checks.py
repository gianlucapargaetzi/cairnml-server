"""The rules that decide whether a device or a job token may be used on this request.

Pure functions: they take the loaded records and decide. The request path loads the records.
"""

from apiserver.database.model.task.task import TaskStatus

DEVICE_KINDS = ("client", "agent")
LIVE_JOB_STATUSES = (TaskStatus.queued, TaskStatus.in_progress)


class AccessDenied(Exception):
    pass


def check_device(device, owner) -> None:
    """A device token works only while its device is not revoked and its owner is enabled."""
    if device is None:
        raise AccessDenied("unknown device")
    if device.revoked:
        raise AccessDenied("device revoked")
    if owner is None:
        raise AccessDenied("unknown owner")
    if owner.idp_disabled:
        raise AccessDenied("owner disabled")


def check_job(task_status, owner) -> None:
    """A job token works only while its task is queued or running and its owner is enabled."""
    if task_status is None:
        raise AccessDenied("unknown task")
    if task_status not in LIVE_JOB_STATUSES:
        raise AccessDenied(f"task is {task_status}")
    if owner is None or owner.idp_disabled:
        raise AccessDenied("owner disabled or unknown")


def check_user(owner) -> None:
    """A web session works only while its user is enabled."""
    if owner is None or owner.idp_disabled:
        raise AccessDenied("user disabled or unknown")
