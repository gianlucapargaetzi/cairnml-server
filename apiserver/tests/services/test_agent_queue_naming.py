"""An agent's queue is named after its machine, with a short hash of its owner: the same owner and machine get the same
queue back, and two owners with machines of one name get different queues."""
from datetime import datetime
from uuid import uuid4

from apiserver.database.model.queue import Queue
from apiserver.services import devices
from apiserver.tests.db import ensure_db

ensure_db()


def test_the_queue_id_is_the_machine_name_and_a_short_hash_of_the_owner():
    queue_id = devices.agent_queue_id("u1", "GPU Server 01")
    assert queue_id.startswith("gpu-server-01-") and len(queue_id.rsplit("-", 1)[1]) == 6


def test_the_same_owner_and_machine_get_the_same_queue_id():
    assert devices.agent_queue_id("u1", "gpu-01") == devices.agent_queue_id("u1", "gpu-01")


def test_two_owners_with_a_machine_of_one_name_get_different_queues():
    assert devices.agent_queue_id("u1", "gpu-01") != devices.agent_queue_id("u2", "gpu-01")


def test_enrolling_the_same_machine_again_reuses_its_queue():
    company = f"c-{uuid4().hex[:8]}"
    now = datetime.utcnow()
    first = devices._agent_queue(company, "u1", "gpu-01", now)
    first.save()
    again = devices._agent_queue(company, "u1", "gpu-01", now)
    assert again.id == first.id
    Queue.objects(company=company).delete()


def test_a_queue_taken_by_someone_else_is_avoided_with_a_longer_hash():
    company = f"c-{uuid4().hex[:8]}"
    now = datetime.utcnow()
    taken = devices.agent_queue_id("u1", "gpu-01")
    Queue(id=taken, company=company, name=taken, created=now, created_by="someone-else").save()
    mine = devices._agent_queue(company, "u1", "gpu-01", now)
    assert mine.id != taken and mine.id.startswith("gpu-01-") and len(mine.id.rsplit("-", 1)[1]) > 6
    Queue.objects(company=company).delete()
