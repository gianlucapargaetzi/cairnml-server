"""The per-event steps of EventBLL.add_events as functions that can be tested alone."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from nested_dict import nested_dict

from apiserver.apierrors import errors
from apiserver.bll.event.event_common import EventType, get_index_name
from apiserver.database import utils as dbutils
from apiserver.es_factory import es_factory

# noinspection PyTypeChecker
EVENT_TYPES: Set[str] = set(et.value for et in EventType if et != EventType.all)
MAX_LONG = 2**63 - 1
MIN_LONG = -(2**63)
INVALID_ITERATION_ERROR = f"Iteration number should not exceed {MAX_LONG}"


class IterationOutOfRange(Exception):
    """The iteration of an event does not fit a long integer."""


@dataclass
class EventBatch:
    """What add_events collects while it prepares the events, for the write and the statistics updates."""

    actions: List[dict] = field(default_factory=list)
    used_task_ids: Set[str] = field(default_factory=set)
    used_model_ids: Set[str] = field(default_factory=set)
    task_iteration: Dict[str, int] = field(default_factory=lambda: defaultdict(lambda: 0))
    # task_id -> metric_hash -> variant_hash -> MetricEvent
    task_last_scalar_events: dict = field(default_factory=lambda: nested_dict(3, dict))
    # task_id -> metric_hash -> event_type -> MetricEvent
    task_last_events: dict = field(default_factory=lambda: nested_dict(3, dict))
    errors_per_type: Dict[str, int] = field(default_factory=lambda: defaultdict(int))


def collect_entity_ids(events: Sequence[dict]) -> Tuple[Dict[str, bool], Dict[str, bool]]:
    """The task ids and the model ids the events refer to, each mapped to its allow_locked flag.

    Takes allow_locked out of every event and sets model_event to False on task events, in place; a model event's
    "model" is moved into "task". Refuses events that give one id two allow_locked settings and ids used as both
    task and model.
    """
    task_ids = {}
    model_ids = {}
    for event in events:
        if event.get("model_event", False):
            model = event.pop("model", None)
            if model is not None:
                event["task"] = model
            entity_ids = model_ids
        else:
            event["model_event"] = False
            entity_ids = task_ids

        id_ = event.get("task")
        allow_locked = event.pop("allow_locked", False)
        if not id_:
            continue

        allowed_for_entity = entity_ids.get(id_)
        if allowed_for_entity is None:
            entity_ids[id_] = allow_locked
        elif allowed_for_entity != allow_locked:
            raise errors.bad_request.ValidationError(
                f"Inconsistent allow_locked setting in the passed events for {id_}"
            )

    found_in_both = set(task_ids).intersection(set(model_ids))
    if found_in_both:
        raise errors.bad_request.ValidationError(
            "Inconsistent model_event setting in the passed events",
            tasks=found_in_both,
        )
    return task_ids, model_ids


def check_event(event: dict, valid_tasks: Set[str], valid_models: Set[str]) -> Tuple[Optional[str], Optional[str]]:
    """(error, event type): the message the event is refused with, or None, and its type with the spaces replaced.
    The checks run in a fixed order: type, known type, task logs for models, task id, validity of the entity."""
    event_type = event.get("type")
    if event_type is None:
        return "Event must have a 'type' field", None

    event_type = event_type.replace(" ", "_")
    if event_type not in EVENT_TYPES:
        return f"Invalid event type {event_type}", event_type

    model_event = event["model_event"]
    if model_event and event_type == EventType.task_log.value:
        return "Task log events are not supported for models", event_type

    task_or_model_id = event.get("task")
    if task_or_model_id is None:
        return "Event must have a 'task' field", event_type

    valid = valid_models if model_event else valid_tasks
    if task_or_model_id not in valid:
        return (
            f"Invalid {'model' if model_event else 'task'} id {task_or_model_id}",
            event_type,
        )
    return None, event_type


def normalise_event(event: dict, event_type: str, worker: str) -> Optional[int]:
    """Bring the event into the form it is indexed in (in place) and return its iteration.

    Sets the type, @timestamp (the write time), timestamp and worker unless sent, turns ts into timestamp and values
    into value, forces iter to an int and defaults metric and variant to "". Raises IterationOutOfRange for an
    iteration that does not fit a long integer; the event is then left half prepared and must be dropped.
    """
    event["type"] = event_type

    # @timestamp indicates the time the event is written, not when it happened
    event["@timestamp"] = es_factory.get_es_timestamp_str()

    # for backward bomba-tavili-tea
    if "ts" in event:
        event["timestamp"] = event.pop("ts")

    # set timestamp and worker if not sent
    if "timestamp" not in event:
        event["timestamp"] = es_factory.get_timestamp_millis()

    if "worker" not in event:
        event["worker"] = worker

    # force iter to be a long int
    iteration = event.get("iter")
    if iteration is not None:
        iteration = int(iteration)
        if iteration > MAX_LONG or iteration < MIN_LONG:
            raise IterationOutOfRange()
        event["iter"] = iteration

    # used to have "values" to indicate array. no need anymore
    if "values" in event:
        event["value"] = event["values"]
        del event["values"]

    event["metric"] = event.get("metric") or ""
    event["variant"] = event.get("variant") or ""
    return iteration


def make_action(company_id: str, event: dict, event_type: str, get_event_id: Callable[[dict], str]) -> dict:
    """The Elasticsearch bulk action that indexes the event."""
    es_action = {
        "_op_type": "index",  # overwrite if exists with same ID
        "_index": get_index_name(company_id, event_type),
        "_source": event,
    }

    # for "log" events, don't assign custom _id - whatever is sent, is written (not overwritten)
    if event_type != EventType.task_log.value:
        es_action["_id"] = get_event_id(event)
    else:
        es_action["_id"] = dbutils.id()
    return es_action
