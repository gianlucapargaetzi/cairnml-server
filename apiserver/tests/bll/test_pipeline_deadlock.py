"""A controller on the only agent of a queue must not queue its steps there."""

from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.bll.queue import pipeline_deadlock
from apiserver.bll.queue.pipeline_deadlock import assert_not_deadlocked, blocks_its_own_steps

QUEUE = "queue-1"
JOB = "controller-task"


def worker(queues, task=None):
    return SimpleNamespace(queues=queues, task=SimpleNamespace(id=task) if task else None)


def test_the_only_agent_of_the_queue_runs_the_controller():
    assert blocks_its_own_steps([worker([QUEUE], JOB)], QUEUE, JOB)


def test_a_second_agent_on_the_queue_is_no_deadlock():
    assert not blocks_its_own_steps([worker([QUEUE], JOB), worker([QUEUE])], QUEUE, JOB)


def test_an_agent_of_another_queue_does_not_count():
    assert blocks_its_own_steps([worker([QUEUE], JOB), worker(["other"])], QUEUE, JOB)


def test_the_only_agent_running_another_task_is_no_deadlock():
    assert not blocks_its_own_steps([worker([QUEUE], "someone-elses"), worker(["other"], JOB)], QUEUE, JOB)
    assert not blocks_its_own_steps([worker([QUEUE])], QUEUE, JOB)
    assert not blocks_its_own_steps([], QUEUE, JOB)


def test_a_request_that_is_not_made_by_a_job_is_not_checked():
    call = SimpleNamespace(auth=SimpleNamespace(job_task=None))
    assert_not_deadlocked(call, None, "company", "t", QUEUE, None)  # no database is read


def test_the_refusal_says_what_to_do(monkeypatch):
    class Queue:
        @staticmethod
        def objects(**kwargs):
            return SimpleNamespace(only=lambda *a: SimpleNamespace(first=lambda: SimpleNamespace(id=QUEUE, name="my-queue")))

    monkeypatch.setattr("apiserver.database.model.queue.Queue", Queue)
    bll = SimpleNamespace(worker_bll=SimpleNamespace(get_all=lambda company: [worker([QUEUE], JOB)]))
    call = SimpleNamespace(auth=SimpleNamespace(job_task=JOB))
    with pytest.raises(errors.bad_request.ValidationError) as caught:
        assert_not_deadlocked(call, bll, "company", "step", QUEUE, None)
    assert "dead-locked" in str(caught.value) and "more than one agent" in str(caught.value)
    bll.worker_bll.get_all = lambda company: [worker([QUEUE], JOB), worker([QUEUE])]
    assert_not_deadlocked(call, bll, "company", "step", QUEUE, None)
