"""Outbound webhooks for tasks. A project may name one https URL. When a task of the project completes, fails or is
stopped (a stopped task is what an aborted one becomes), the URL receives a POST with a JSON body:

    {"project": name, "task_id": id, "name": task name, "status": status, "duration": seconds, "link": web app link}

The body holds no parameters and no secrets. The change of status is not held up: the delivery runs on a worker
thread. A failed attempt is retried MAX_RETRIES times, with a doubling pause. Each delivery writes one audit event,
webhook.sent or webhook.failed, with the project and the HTTP status code (None when no response came).

The address rules are those of the repository probe (apiserver.idp.repo_visibility): the name is resolved once, every
address it resolves to is checked, the request connects to the checked address, and redirects are not followed. A
refused address is never retried. The URL itself is not logged: it may carry a secret token.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import urlsplit

import requests

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import TaskStatus
from apiserver.idp import audit, public_origin
from apiserver.idp.repo_visibility import (
    PinnedAdapter,
    ProbeBlocked,
    ProbeFailed,
    check_address,
)

log = config.logger(__file__)

MAX_RETRIES = 3
BACKOFF_SEC = 2  # the pause before the first retry, doubled before each further one
TIMEOUT_SEC = 10
MAX_URL_LENGTH = 2048
ACTOR = "system"
NOTIFY_STATUSES = frozenset({TaskStatus.completed, TaskStatus.failed, TaskStatus.stopped})

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="task-webhook")


def url_problem(url: str) -> Optional[str]:
    """What is wrong with a webhook URL, or None when it may be stored. Plain http is refused unless the host is
    listed in apiserver.webhooks.insecure_http_hosts (local test receivers only)."""
    if len(url) > MAX_URL_LENGTH:
        return f"is longer than {MAX_URL_LENGTH} characters"
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        return "must not contain spaces or control characters"
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        return "is not a valid URL"
    if parts.scheme not in ("https", "http"):
        return "must be an https URL"
    if not parts.hostname:
        return "must name a host"
    if "@" in parts.netloc:
        return "must not contain credentials"
    insecure = {str(host).lower() for host in config.get("apiserver.webhooks.insecure_http_hosts", []) or []}
    if parts.scheme == "http" and parts.hostname not in insecure:
        return "must be an https URL"
    return None


def validate_url(url: str) -> None:
    problem = url_problem(url)
    if problem:
        raise errors.bad_request.ValidationError(f"notification_webhook {problem}")


def duration_sec(started: Optional[datetime], completed: Optional[datetime]) -> Optional[int]:
    if not started or not completed:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    return max(0, int((completed - started).total_seconds()))


def build_payload(project: str, task_id: str, name: str, status: str, duration: Optional[int], link: Optional[str]) -> dict:
    return {
        "project": project,
        "task_id": task_id,
        "name": name,
        "status": status,
        "duration": duration,
        "link": link,
    }


def task_link(project_id: str, task_id: str) -> Optional[str]:
    """The web app's page of the task. Outside a request (a worker changed the status) only a configured or pinned
    address is known, otherwise there is no link."""
    try:
        base = public_origin.web_url()
    except RuntimeError:
        return None
    return f"{base.rstrip('/')}/projects/{project_id}/tasks/{task_id}"


def notify_status_change(task, new_status: str, completed: datetime) -> None:
    """Queues the delivery when a task changed to a status that is notified. Never raises: a webhook must not make a
    status change fail."""
    if new_status not in NOTIFY_STATUSES or not task.project:
        return
    try:
        job = dict(
            company=task.company,
            project=task.project,
            task_id=task.id,
            name=task.name,
            status=new_status,
            duration=duration_sec(task.started, completed),
            link=task_link(task.project, task.id),
        )
        _pool.submit(deliver, job)
    except Exception:
        log.exception("could not queue the webhook of task %s", task.id)


def send(url: str, payload: dict) -> int:
    """Posts the payload to the checked address of the URL and returns the HTTP status code. Raises ProbeBlocked or
    ProbeFailed when the address is refused, and requests.RequestException when no response came."""
    parts = urlsplit(url)
    scheme, name = parts.scheme, parts.hostname
    port = parts.port or (443 if scheme == "https" else 80)
    address = check_address(name, port)
    literal = f"[{address}]" if ":" in address else address
    target = f"{scheme}://{literal}:{port}{parts.path or '/'}"
    if parts.query:
        target += f"?{parts.query}"
    session = requests.Session()
    session.trust_env = False
    session.mount(f"{scheme}://", PinnedAdapter(name, tls=scheme == "https"))
    try:
        response = session.post(
            target,
            json=payload,
            headers={"Host": parts.netloc},
            timeout=TIMEOUT_SEC,
            allow_redirects=False,
        )
        return response.status_code
    finally:
        session.close()


def deliver_with_retries(url: str, payload: dict) -> Tuple[bool, Optional[int]]:
    """Makes the first attempt and up to MAX_RETRIES retries. Returns (sent, last HTTP status code or None)."""
    code = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            time.sleep(BACKOFF_SEC * 2 ** (attempt - 1))
        try:
            code = send(url, payload)
        except (ProbeBlocked, ProbeFailed) as exc:
            log.info("webhook not sent: %s", exc.__class__.__name__)
            return False, None
        except requests.RequestException:
            code = None
            continue
        if 200 <= code < 300:
            return True, code
    return False, code


def deliver(job: dict) -> None:
    """Runs on a worker thread: looks up the project's webhook, delivers it and writes the audit event."""
    try:
        project = (
            Project.objects(company=job["company"], id=job["project"])
            .only("name", "notification_webhook")
            .first()
        )
        url = project.notification_webhook if project else None
        if not url:
            return
        payload = build_payload(
            project=project.name,
            task_id=job["task_id"],
            name=job["name"],
            status=job["status"],
            duration=job["duration"],
            link=job["link"],
        )
        sent, code = deliver_with_retries(url, payload)
        audit.record(
            ACTOR,
            "webhook.sent" if sent else "webhook.failed",
            company=job["company"],
            project=job["project"],
            status_code=code,
        )
    except Exception:
        log.exception("webhook delivery for task %s failed", job.get("task_id"))
