"""The queue of an agent: one per owner and machine, so that an agent that enrols again gets the queue it had."""

import hashlib
import re
from datetime import datetime

from apiserver.apierrors import errors
from apiserver.database.model.queue import Queue


def agent_queue_id(owner: str, machine: str, length: int = 6) -> str:
    """The id of an agent's queue: the machine's name, then a short hash of its owner and the name. The same owner and
    machine always get the same queue, and two owners with machines of one name get different queues."""
    slug = re.sub(r"[^a-z0-9]+", "-", machine.lower()).strip("-")[:40] or "machine"
    digest = hashlib.sha256(f"{owner}:{machine}".encode("utf-8")).hexdigest()
    return f"{slug}-{digest[:length]}"


def allocate_agent_queue(company: str, owner: str, machine: str, now: datetime) -> Queue:
    """The agent's own queue: the one its owner has for this machine (reused when the machine is enrolled again), or a new
    one, unpublished, owned by the owner. A hash taken by someone else's queue is lengthened."""
    for length in range(6, 13):
        queue_id = agent_queue_id(owner, machine, length)
        existing = Queue.objects(id=queue_id).first()
        if existing is None:
            return Queue(
                id=queue_id,
                company=company,
                name=queue_id,
                display_name=machine,
                created=now,
                created_by=owner,
                public=False,
            )
        if existing.company == company and existing.created_by == owner:
            return existing
    raise errors.bad_request.ValidationError(f"no queue id is free for the machine {machine}")
