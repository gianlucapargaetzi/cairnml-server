"""Characterisation tests for EventBLL.add_events: they pin what the function sends to Elasticsearch, writes to
MongoDB, returns and raises today. Elasticsearch is replaced by a recording stub of elasticsearch.helpers.streaming_bulk;
tasks and models are real MongoDB documents.

Behaviour groups: one scalar event (action shape, chunk size, refresh), type normalisation, timestamps and worker,
iteration rewrite and range, values rewrite, log ids versus keyed ids, error buckets (type, task, validity, locked),
allow_locked consistency, task/model clash, partial success, EventsNotAdded, failed bulk items, plot compression and
validation, last iteration, scalar statistics, model events, set_last_update path, in-place mutation of the events.
"""

from datetime import datetime
from unittest import mock
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll.event import event_bll as event_bll_module
from apiserver.bll.event.event_bll import EventBLL, MAX_LONG, MIN_LONG
from apiserver.bll.event.event_common import get_index_name
from apiserver.database.model.auth import Role
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.service_repo.auth import Identity
from apiserver.tests.db import ensure_db

ensure_db()

TS = 1_700_000_000_000
ES_TS = "2024-01-02T03:04:05.006Z"


@pytest.fixture(scope="module", autouse=True)
def _warm_up_indexes():
    """The first use of the Task model in a process can fail on a text index left by another version of the model
    in a shared database (IndexOptionsConflict). Take that hit here, not in the first test."""
    for cls in (Task, Model):
        try:
            cls.objects.first()
        except Exception:
            pass


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


class Bulk:
    """Stub of elasticsearch.helpers.streaming_bulk that records what it gets and can fail chosen actions."""

    def __init__(self):
        self.calls = []
        self.failing = set()

    @property
    def actions(self):
        return [a for call in self.calls for a in call["actions"]]

    def __call__(self, client, actions, **kwargs):
        actions = list(actions)
        self.calls.append(dict(client=client, actions=actions, kwargs=kwargs))
        for action in actions:
            yield (action["_id"] not in self.failing), {}


@pytest.fixture
def bulk():
    stub = Bulk()
    with (
        mock.patch("elasticsearch.helpers.streaming_bulk", stub),
        mock.patch.object(event_bll_module.es_factory, "get_es_timestamp_str", return_value=ES_TS),
        mock.patch.object(event_bll_module.es_factory, "get_timestamp_millis", return_value=TS),
    ):
        yield stub


@pytest.fixture
def cfg():
    """Overrides for config.get as event_bll reads it per call."""
    overrides = {}
    original = event_bll_module.config.get

    def fake(key, default=None):
        return overrides.get(key, original(key, default))

    with mock.patch.object(event_bll_module.config, "get", side_effect=fake):
        yield overrides


def new_id(prefix):
    return f"{prefix}-{uuid4().hex[:8]}"


def make_task(company, status=TaskStatus.created, **kw):
    task = Task(
        id=new_id("t"),
        company=company,
        user="u1",
        name="task",
        type="training",
        created=datetime.utcnow(),
        status=status,
        **kw,
    )
    task.save()
    return task.id


def make_model(company, ready=False):
    model = Model(
        id=new_id("m"),
        company=company,
        user="u1",
        name="model",
        uri="s3://m",
        created=datetime.utcnow(),
        ready=ready,
    )
    model.save()
    return model.id


def bll(skip_iteration=()):
    instance = object.__new__(EventBLL)
    instance.es = mock.MagicMock()
    instance._skip_iteration_for_metric = set(skip_iteration)
    return instance


def add(company, events, worker="w1", instance=None):
    ident = Identity(user="u1", company=company, role=Role.user)
    return (instance or bll()).add_events(company, ident, events, worker)


def scalar(task, **kw):
    return dict(type="training_stats_scalar", task=task, iter=1, metric="loss", variant="l", value=0.5, **kw)


def row(task_id):
    return Task.objects(id=task_id).first()


# --- one scalar event -----------------------------------------------------


def test_one_scalar_event_is_indexed_and_counted(company, bulk):
    task = make_task(company)
    result = add(company, [scalar(task)])
    assert result == (1, 0, {})
    (action,) = bulk.actions
    assert action["_op_type"] == "index"
    assert action["_index"] == get_index_name(company, "training_stats_scalar")
    source = action["_source"]
    assert source["@timestamp"] == ES_TS and source["timestamp"] == TS
    assert source["worker"] == "w1" and source["model_event"] is False
    assert (source["task"], source["iter"], source["metric"], source["variant"], source["value"]) == (
        task,
        1,
        "loss",
        "l",
        0.5,
    )


def test_the_bulk_call_gets_a_chunk_size_of_500_and_refresh(company, bulk):
    add(company, [scalar(make_task(company))])
    assert bulk.calls[0]["kwargs"] == {"chunk_size": 500, "refresh": True}


def test_the_bulk_call_is_not_made_without_actions(company, bulk):
    with pytest.raises(errors.bad_request.EventsNotAdded):
        add(company, [dict(type="training_stats_scalar", task="nope")])
    assert bulk.calls == []


def test_the_id_is_the_md5_of_the_present_key_parts(company, bulk):
    import hashlib

    task = make_task(company)
    add(company, [scalar(task)])
    assert bulk.actions[0]["_id"] == hashlib.md5(f"{task}-1-loss-l".encode()).hexdigest()


def test_the_id_skips_absent_parts(company, bulk):
    import hashlib

    task = make_task(company)
    add(company, [dict(type="training_stats_scalar", task=task, value=1)])
    # metric and variant are always set (empty), iter is absent
    assert bulk.actions[0]["_id"] == hashlib.md5(f"{task}--".encode()).hexdigest()


def test_the_same_key_gives_the_same_id_for_non_log_events(company, bulk):
    task = make_task(company)
    add(company, [scalar(task), scalar(task)])
    assert bulk.actions[0]["_id"] == bulk.actions[1]["_id"]


def test_log_events_get_a_fresh_random_id_each_time(company, bulk):
    task = make_task(company)
    log = dict(type="log", task=task, msg="m")
    add(company, [dict(log), dict(log)])
    ids = [a["_id"] for a in bulk.actions]
    assert len(set(ids)) == 2 and all(len(i) == 32 for i in ids)


def test_metric_and_variant_default_to_empty_strings(company, bulk):
    task = make_task(company)
    add(company, [dict(type="log", task=task, metric=None)])
    source = bulk.actions[0]["_source"]
    assert source["metric"] == "" and source["variant"] == ""


def test_a_sent_worker_and_timestamp_are_kept(company, bulk):
    task = make_task(company)
    add(company, [scalar(task, worker="mine", timestamp=5)])
    source = bulk.actions[0]["_source"]
    assert source["worker"] == "mine" and source["timestamp"] == 5


def test_ts_becomes_timestamp_and_wins_over_a_sent_timestamp(company, bulk):
    task = make_task(company)
    add(company, [scalar(task, ts=7, timestamp=5)])
    source = bulk.actions[0]["_source"]
    assert source["timestamp"] == 7 and "ts" not in source


def test_values_becomes_value(company, bulk):
    task = make_task(company)
    event = dict(type="training_stats_scalar", task=task, values=2.5)
    add(company, [event])
    source = bulk.actions[0]["_source"]
    assert source["value"] == 2.5 and "values" not in source


def test_the_event_type_has_spaces_replaced(company, bulk):
    task = make_task(company)
    add(company, [dict(type="training stats scalar", task=task, value=1)])
    assert bulk.actions[0]["_source"]["type"] == "training_stats_scalar"
    assert bulk.actions[0]["_index"] == get_index_name(company, "training_stats_scalar")


# --- iteration ------------------------------------------------------------


def test_a_string_iteration_becomes_an_int(company, bulk):
    task = make_task(company)
    add(company, [scalar(task, **{}) | {"iter": "12"}])
    assert bulk.actions[0]["_source"]["iter"] == 12


def test_an_event_without_iter_is_written_without_iter(company, bulk):
    task = make_task(company)
    event = scalar(task)
    del event["iter"]
    add(company, [event])
    assert "iter" not in bulk.actions[0]["_source"]


def test_an_out_of_range_iteration_is_not_written_and_raises_after_the_rest_is_done(company, bulk):
    good = make_task(company)
    bad = make_task(company)
    events = [scalar(good) | {"iter": 3}, scalar(bad) | {"iter": MAX_LONG + 1}]
    with pytest.raises(errors.server_error.DataError) as exc:
        add(company, events)
    assert [a["_source"]["task"] for a in bulk.actions] == [good]
    # the rest was written and the task rows were updated before the raise
    assert row(good).last_iteration == 3
    assert exc.value.codes == (500, 100)
    assert "1 document(s) failed to index." in exc.value.msg


def test_the_iteration_bounds(company, bulk):
    task = make_task(company)
    add(company, [scalar(task) | {"iter": MAX_LONG}, scalar(task) | {"iter": MIN_LONG}])
    assert [a["_source"]["iter"] for a in bulk.actions] == [MAX_LONG, MIN_LONG]
    with pytest.raises(errors.server_error.DataError):
        add(company, [scalar(task) | {"iter": MIN_LONG - 1}])


def test_the_last_iteration_is_the_max_of_the_sent_iterations(company, bulk):
    task = make_task(company)
    add(company, [scalar(task) | {"iter": 4}, scalar(task) | {"iter": 9}, scalar(task) | {"iter": 2}])
    assert row(task).last_iteration == 9


def test_metrics_in_the_skip_set_do_not_move_the_last_iteration(company, bulk):
    task = make_task(company)
    start = row(task).last_iteration
    add(company, [scalar(task) | {"iter": 50, "metric": ":monitor:gpu"}], instance=bll([":monitor:gpu"]))
    assert row(task).last_iteration == start
    add(company, [scalar(task) | {"iter": 5}], instance=bll([":monitor:gpu"]))
    assert row(task).last_iteration == 5


# --- error buckets --------------------------------------------------------


def test_errors_are_bucketed_by_message_with_counts(company, bulk):
    task = make_task(company)
    events = [
        scalar(task),
        {"task": task},
        {"task": task},
        dict(type="nope", task=task),
        dict(type="log", task=None),
        dict(type="log", task="missing-task", msg="x"),
    ]
    added, error_count, buckets = add(company, events)
    assert added == 1 and error_count == 5
    assert buckets == {
        "Event must have a 'type' field": 2,
        "Invalid event type nope": 1,
        "Event must have a 'task' field": 1,
        "Invalid task id missing-task": 1,
    }


def test_task_log_events_are_refused_for_models(company, bulk):
    model = make_model(company)
    task = make_task(company)
    added, errors_count, buckets = add(company, [scalar(task), dict(type="log", task=model, model_event=True, msg="x")])
    assert buckets == {"Task log events are not supported for models": 1}


def test_an_unknown_task_is_an_invalid_task_id(company, bulk):
    task = make_task(company)
    _, _, buckets = add(company, [scalar(task), scalar("other-task-id")])
    assert buckets == {"Invalid task id other-task-id": 1}


def test_a_task_of_another_company_is_an_invalid_task_id(company, bulk):
    mine = make_task(company)
    theirs = make_task(f"other-{uuid4().hex[:6]}")
    _, _, buckets = add(company, [scalar(mine), scalar(theirs)])
    assert buckets == {f"Invalid task id {theirs}": 1}


@pytest.mark.parametrize("status", [TaskStatus.published, TaskStatus.publishing])
def test_a_locked_task_is_invalid_without_allow_locked(company, bulk, status):
    mine = make_task(company)
    locked = make_task(company, status=status)
    _, _, buckets = add(company, [scalar(mine), scalar(locked)])
    assert buckets == {f"Invalid task id {locked}": 1}


def test_a_locked_task_is_accepted_with_allow_locked(company, bulk):
    mine = make_task(company)
    locked = make_task(company, status=TaskStatus.published)
    added, _, buckets = add(company, [scalar(mine), scalar(locked, allow_locked=True)])
    assert added == 2 and buckets == {}


def test_allow_locked_is_removed_from_the_events_even_without_an_id(company, bulk):
    task = make_task(company)
    events = [scalar(task), {"type": "log", "allow_locked": True}]
    add(company, events)
    assert "allow_locked" not in events[1] and "allow_locked" not in bulk.actions[0]["_source"]


def test_inconsistent_allow_locked_for_one_task_is_refused(company, bulk):
    task = make_task(company)
    with pytest.raises(errors.bad_request.ValidationError) as exc:
        add(company, [scalar(task), scalar(task, allow_locked=True)])
    assert f"Inconsistent allow_locked setting in the passed events for {task}" in str(exc.value)
    assert bulk.calls == []


def test_one_id_used_as_task_and_model_is_refused(company, bulk):
    task = make_task(company)
    with pytest.raises(errors.bad_request.ValidationError) as exc:
        add(company, [scalar(task), scalar(task) | {"model_event": True}])
    assert "Inconsistent model_event setting in the passed events" in str(exc.value)
    assert bulk.calls == []


def test_partial_success_returns_counts_and_buckets(company, bulk):
    task = make_task(company)
    assert add(company, [scalar(task), {"task": task}]) == (
        1,
        1,
        {"Event must have a 'type' field": 1},
    )


def test_no_event_added_raises_events_not_added_with_the_buckets(company, bulk):
    with pytest.raises(errors.bad_request.EventsNotAdded) as exc:
        add(company, [{"task": "x"}])
    assert exc.value.msg == "Events not added: Event must have a 'type' field=1"
    assert exc.value.codes == (400, 131)


def test_an_empty_list_raises_events_not_added(company, bulk):
    with pytest.raises(errors.bad_request.EventsNotAdded):
        add(company, [])


def test_a_failed_bulk_item_is_an_error_and_not_counted(company, bulk):
    task = make_task(company)
    ok = scalar(task) | {"iter": 1}
    failing = scalar(task) | {"iter": 2}
    import hashlib

    bulk.failing.add(hashlib.md5(f"{task}-2-loss-l".encode()).hexdigest())
    assert add(company, [ok, failing]) == (1, 1, {"Error when indexing events batch": 1})


def test_the_events_passed_in_are_changed_in_place(company, bulk):
    task = make_task(company)
    event = scalar(task, ts=3, allow_locked=False, x_axis_label="x")
    add(company, [event])
    assert event["model_event"] is False and event["timestamp"] == 3
    assert "ts" not in event and "allow_locked" not in event and "x_axis_label" not in event
    assert event["@timestamp"] == ES_TS and event["worker"] == "w1"


# --- plots ----------------------------------------------------------------


def plot(task, text):
    return dict(type="plot", task=task, iter=1, metric="m", variant="v", plot_str=text)


def test_a_small_plot_is_kept_with_its_length_and_source_urls(company, bulk, cfg):
    task = make_task(company)
    text = '{"source": "s3://bucket/img.png"}'
    add(company, [plot(task, text)])
    source = bulk.actions[0]["_source"]
    assert source["plot_str"] == text and source["plot_len"] == len(text)
    assert source["source_urls"] == ["s3://bucket/img.png"]
    assert "plot_data" not in source and "valid_plot" not in source


def test_a_plot_over_the_threshold_is_compressed(company, bulk, cfg):
    import base64
    import zlib

    cfg["services.events.plot_compression_threshold"] = 10
    task = make_task(company)
    text = "x" * 50
    add(company, [plot(task, text)])
    source = bulk.actions[0]["_source"]
    assert "plot_str" not in source and source["plot_len"] == 50
    assert zlib.decompress(base64.b64decode(source["plot_data"])).decode() == text


def test_a_plot_without_text_gets_a_zero_length(company, bulk, cfg):
    task = make_task(company)
    add(company, [dict(type="plot", task=task)])
    assert bulk.actions[0]["_source"]["plot_len"] == 0


def test_plot_validation_marks_valid_and_invalid_plots(company, bulk, cfg):
    cfg["services.events.validate_plot_str"] = True
    task = make_task(company)
    add(company, [plot(task, '{"a": 1}') | {"iter": 1}, plot(task, "not json") | {"iter": 2}])
    valid = {a["_source"]["iter"]: a["_source"]["valid_plot"] for a in bulk.actions}
    assert valid == {1: True, 2: False}


def test_skip_validation_is_removed_and_skips_the_check(company, bulk, cfg):
    cfg["services.events.validate_plot_str"] = True
    task = make_task(company)
    add(company, [plot(task, "not json") | {"skip_validation": True}])
    source = bulk.actions[0]["_source"]
    assert "skip_validation" not in source and "valid_plot" not in source


def test_skip_validation_is_not_removed_when_validation_is_off(company, bulk, cfg):
    task = make_task(company)
    add(company, [plot(task, "{}") | {"skip_validation": True}])
    assert "skip_validation" in bulk.actions[0]["_source"]


# --- task statistics ------------------------------------------------------


def test_scalar_statistics_land_on_the_task(company, bulk):
    task = make_task(company)
    events = [
        scalar(task) | {"iter": 1, "value": 3.0, "timestamp": 10},
        scalar(task) | {"iter": 2, "value": 1.0, "timestamp": 20, "x_axis_label": "ep"},
        scalar(task) | {"iter": 3, "value": 5.0, "timestamp": 30},
    ]
    add(company, events)
    from apiserver.database import utils as dbutils

    m = row(task).last_metrics[dbutils.hash_field_name("loss")][dbutils.hash_field_name("l")]
    assert (m.value, m.metric, m.variant) == (5.0, "loss", "l")
    assert (m.min_value, m.min_value_iteration, m.max_value, m.max_value_iteration) == (1.0, 2, 5.0, 3)
    assert (m.first_value, m.first_value_iteration) == (3.0, 1)
    assert m.count == 3 and m.mean_value == 3.0


def test_x_axis_label_is_kept_on_the_last_value(company, bulk):
    task = make_task(company)
    from apiserver.database import utils as dbutils

    add(company, [scalar(task) | {"timestamp": 10, "x_axis_label": "ep"}])
    m = row(task).last_metrics[dbutils.hash_field_name("loss")][dbutils.hash_field_name("l")]
    assert m.x_axis_label == "ep"


def test_the_task_rows_get_last_update_and_last_changed_by(company, bulk):
    task = make_task(company)
    before = row(task).last_update
    add(company, [scalar(task)])
    after = row(task)
    assert after.last_changed_by == "u1" and after.last_update != before


def test_a_plot_event_updates_the_task_through_update_statistics_not_set_last_update(company, bulk):
    task = make_task(company)
    with (
        mock.patch.object(event_bll_module.TaskBLL, "set_last_update") as set_last_update,
        mock.patch.object(event_bll_module.TaskBLL, "update_statistics") as update_statistics,
    ):
        add(company, [dict(type="plot", task=task, plot_str="{}")])
    update_statistics.assert_called_once()  # the metric event record counts as last_events
    assert set_last_update.call_count == 0


def test_update_task_with_nothing_to_update_returns_false_and_triggers_set_last_update(company, bulk):
    task = make_task(company)
    with (
        mock.patch.object(event_bll_module.TaskBLL, "set_last_update") as set_last_update,
        mock.patch.object(EventBLL, "_update_task", return_value=False),
    ):
        add(company, [scalar(task)])
    args, kwargs = set_last_update.call_args
    assert args[0] == {task} and kwargs["company_id"] == company and kwargs["user_id"] == "u1"


def test_task_statistics_are_updated_even_when_the_bulk_item_failed(company, bulk):
    import hashlib

    task = make_task(company)
    bulk.failing.add(hashlib.md5(f"{task}-7-loss-l".encode()).hexdigest())
    with pytest.raises(errors.bad_request.EventsNotAdded):
        add(company, [scalar(task) | {"iter": 7}])
    assert row(task).last_iteration == 7


# --- model events ---------------------------------------------------------


def model_scalar(model, **kw):
    return dict(
        type="training_stats_scalar", model=model, model_event=True, iter=4, metric="m", variant="v", value=2.0, **kw
    )


def test_a_model_event_uses_the_model_as_its_task_id(company, bulk):
    model = make_model(company)
    assert add(company, [model_scalar(model)]) == (1, 0, {})
    source = bulk.actions[0]["_source"]
    assert source["task"] == model and source["model_event"] is True and "model" not in source


def test_a_model_event_updates_the_model_statistics_and_no_task(company, bulk):
    model = make_model(company)
    add(company, [model_scalar(model)])
    saved = Model.objects(id=model).first()
    assert saved.last_iteration == 4
    assert saved.last_metrics


def test_a_ready_model_is_invalid_without_allow_locked(company, bulk):
    model = make_model(company, ready=True)
    task = make_task(company)
    _, _, buckets = add(company, [scalar(task), model_scalar(model)])
    assert buckets == {f"Invalid model id {model}": 1}


def test_a_ready_model_is_accepted_with_allow_locked(company, bulk):
    model = make_model(company, ready=True)
    assert add(company, [model_scalar(model, allow_locked=True)]) == (1, 0, {})


def test_an_unknown_model_is_an_invalid_model_id(company, bulk):
    task = make_task(company)
    _, _, buckets = add(company, [scalar(task), model_scalar("nope")])
    assert buckets == {"Invalid model id nope": 1}


def test_a_model_event_with_a_task_id_set_uses_the_task_field_when_model_is_absent(company, bulk):
    model = make_model(company)
    event = dict(type="training_stats_scalar", task=model, model_event=True, value=1)
    assert add(company, [event]) == (1, 0, {})


# --- collect_entity_ids alone (no database) -------------------------------


def test_collect_entity_ids_alone():
    from apiserver.bll.event.event_prepare import collect_entity_ids

    events = [
        {"task": "t1"},
        {"task": "t1", "allow_locked": False},
        {"task": "t2", "allow_locked": True},
        {"model": "m1", "model_event": True, "allow_locked": True},
        {"task": "m2", "model_event": True},
        {"allow_locked": True},
    ]
    tasks, models = collect_entity_ids(events)
    assert tasks == {"t1": False, "t2": True}
    assert models == {"m1": True, "m2": False}
    assert events[3] == {"task": "m1", "model_event": True}
    assert events[0] == {"task": "t1", "model_event": False}
    assert events[5] == {"model_event": False}


def test_collect_entity_ids_refuses_inconsistent_input():
    from apiserver.bll.event.event_prepare import collect_entity_ids

    with pytest.raises(errors.bad_request.ValidationError):
        collect_entity_ids([{"task": "a"}, {"task": "a", "allow_locked": True}])
    with pytest.raises(errors.bad_request.ValidationError):
        collect_entity_ids([{"task": "a"}, {"task": "a", "model_event": True}])


# --- the per-event steps alone (no database) ------------------------------


def test_check_event_alone():
    from apiserver.bll.event.event_prepare import check_event

    ok = {"type": "training stats scalar", "task": "t", "model_event": False}
    assert check_event(ok, {"t"}, set()) == (None, "training_stats_scalar")
    assert check_event({"model_event": False}, set(), set()) == ("Event must have a 'type' field", None)
    assert check_event({"type": "x", "model_event": False}, set(), set())[0] == "Invalid event type x"
    log = {"type": "log", "task": "m", "model_event": True}
    assert check_event(log, set(), {"m"})[0] == "Task log events are not supported for models"
    assert check_event({"type": "log", "model_event": False}, set(), set())[0] == "Event must have a 'task' field"
    assert check_event({"type": "log", "task": "t", "model_event": False}, set(), set())[0] == "Invalid task id t"
    assert check_event({"type": "plot", "task": "t", "model_event": True}, {"t"}, set())[0] == "Invalid model id t"


def test_normalise_event_alone():
    from apiserver.bll.event.event_prepare import IterationOutOfRange, normalise_event

    event = {"ts": 5, "iter": "3", "values": 1.5, "metric": None}
    with mock.patch.object(event_bll_module.es_factory, "get_es_timestamp_str", return_value=ES_TS):
        assert normalise_event(event, "training_stats_scalar", "w") == 3
    assert event == {
        "type": "training_stats_scalar",
        "@timestamp": ES_TS,
        "timestamp": 5,
        "worker": "w",
        "iter": 3,
        "value": 1.5,
        "metric": "",
        "variant": "",
    }
    with pytest.raises(IterationOutOfRange):
        normalise_event({"iter": MAX_LONG + 1}, "log", "w")


def test_make_action_alone():
    from apiserver.bll.event.event_prepare import make_action

    event = {"task": "t"}
    action = make_action("co", event, "training_stats_scalar", lambda e: "fixed")
    assert action == {
        "_op_type": "index",
        "_index": get_index_name("co", "training_stats_scalar"),
        "_source": event,
        "_id": "fixed",
    }
    assert make_action("co", event, "log", lambda e: "fixed")["_id"] != "fixed"
