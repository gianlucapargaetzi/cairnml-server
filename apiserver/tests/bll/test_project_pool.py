"""A task with no queue goes to the project's queue pool."""

from types import SimpleNamespace

from apiserver.bll.queue.project_pool import choose_queue, pool_queue_stats


def worker(queues, task=None):
    return SimpleNamespace(queues=queues, task=SimpleNamespace(id=task) if task else None)


def test_a_pool_without_queues_has_no_choice():
    assert choose_queue({}) is None


def test_a_queue_with_an_idle_worker_comes_first():
    stats = {"busy": (0, 1, 0), "free": (1, 1, 3)}
    assert choose_queue(stats) == "free"


def test_the_queue_with_the_most_idle_workers_wins():
    assert choose_queue({"a": (1, 2, 0), "b": (2, 2, 5)}) == "b"


def test_without_an_idle_worker_the_shortest_queue_wins():
    assert choose_queue({"a": (0, 1, 4), "b": (0, 1, 1), "c": (0, 1, 2)}) == "b"


def test_a_queue_nobody_serves_is_the_last_choice():
    assert choose_queue({"none": (0, 0, 0), "busy": (0, 1, 9)}) == "busy"


def test_equal_queues_keep_the_order_of_the_allocation():
    assert choose_queue({"first": (1, 1, 0), "second": (1, 1, 0)}) == "first"


def test_the_stats_count_idle_and_all_workers_of_each_queue_and_what_waits():
    workers = [worker(["a"], "t1"), worker(["a"]), worker(["b"]), worker(["elsewhere"])]
    assert pool_queue_stats(["a", "b", "c"], workers, {"a": 2}) == {"a": (1, 2, 2), "b": (1, 1, 0), "c": (0, 0, 0)}
