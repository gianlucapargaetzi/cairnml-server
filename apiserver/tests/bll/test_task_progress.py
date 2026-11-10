import json

import pytest

from apiserver.apierrors import errors
from apiserver.bll.task.progress import EXPIRES_SECONDS, TaskProgress, clean_bars


class FakeRedis:
    def __init__(self):
        self.data, self.expiry = {}, {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key], self.expiry[key] = value, ex

    def mget(self, keys):
        return [self.data.get(k) for k in keys]


BARS = [{"label": "epochs", "n": 3, "total": 10}, {"label": "epoch 4", "n": 120, "total": 391}]


def test_the_latest_value_is_kept_with_an_expiry_and_read_back():
    redis = FakeRedis()
    progress = TaskProgress(redis)
    progress.update("c", "t1", 31, BARS, now=100.0)
    assert list(redis.expiry.values()) == [EXPIRES_SECONDS]
    assert progress.get("c", ["t1", "missing"]) == {"t1": {"percent": 31, "bars": BARS, "updated": 100.0}}


def test_the_database_only_gets_a_coarse_value():
    progress = TaskProgress(FakeRedis())
    assert progress.update("c", "t", 1, BARS) == {"progress": "1", "progress_label": "epochs"}  # the first value
    assert progress.update("c", "t", 2, BARS) is None
    assert progress.update("c", "t", 10, BARS) is None  # only 9 since the last stored one
    assert progress.update("c", "t", 11, BARS) == {"progress": "11", "progress_label": "epochs"}
    assert progress.update("c", "t", 100, BARS) == {"progress": "100", "progress_label": "epochs"}


def test_a_new_outermost_bar_is_stored_at_once():
    progress = TaskProgress(FakeRedis())
    progress.update("c", "t", 100, [{"label": "loading", "n": 5, "total": 5}])
    stored = progress.update("c", "t", 0, [{"label": "epochs", "n": 0, "total": 10}])
    assert stored == {"progress": "0", "progress_label": "epochs"}


def test_without_a_known_total_the_last_percentage_stays():
    progress = TaskProgress(FakeRedis())
    progress.update("c", "t", 40, BARS)
    progress.update("c", "t", None, [{"label": "stream", "n": 7, "total": None}])
    assert progress.get("c", ["t"])["t"]["percent"] == 40


def test_runs_of_other_companies_are_separate():
    progress = TaskProgress(FakeRedis())
    progress.update("c1", "t", 50, BARS)
    assert progress.get("c2", ["t"]) == {}


@pytest.mark.parametrize("percent", [-1, 101])
def test_a_percentage_outside_0_100_is_refused(percent):
    with pytest.raises(errors.bad_request.ValidationError):
        TaskProgress(FakeRedis()).update("c", "t", percent, BARS)


def test_bars_are_cut_and_checked():
    assert len(clean_bars([{"label": str(i), "n": 1, "total": 2} for i in range(9)])) == 4
    assert clean_bars([{"label": "x" * 500}])[0]["label"] == "x" * 100
    with pytest.raises(errors.bad_request.ValidationError):
        clean_bars([{"label": "a", "n": -1}])
    with pytest.raises(errors.bad_request.ValidationError):
        clean_bars(["not a bar"])


def test_a_broken_stored_value_is_ignored():
    redis = FakeRedis()
    redis.data["task_progress_c_t"] = "{not json"
    assert TaskProgress(redis).get("c", ["t"]) == {}
    assert json.loads(json.dumps(TaskProgress(redis).update("c", "t", 5, BARS)))["progress"] == "5"


def test_idle_clears_the_live_value_and_stores_nothing():
    progress = TaskProgress(FakeRedis())
    progress.update("c", "t", 40, BARS)
    assert progress.update("c", "t", None, []) is None  # nothing for the database
    assert progress.get("c", ["t"])["t"]["percent"] is None
    assert progress.get("c", ["t"])["t"]["bars"] == []
    assert progress.update("c", "t", None, None) is None  # no bars at all is idle too
    assert progress.update("c", "t", 5, BARS) == {"progress": "5", "progress_label": "epochs"}  # the next bar is stored
