"""The per-event steps of EventBLL.add_events as functions that can be tested alone."""
from typing import Dict, Sequence, Tuple

from apiserver.apierrors import errors


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
