"""A request whose schema is a choice of shapes is a compound request: its constructor takes the item first, and the
body is the item merged with the request's own options. The item's type constant is declared in the schema (a
single-value enumeration), so the event classes send it without a constructor argument. A compound request whose item
has no name in COMPOUND_ITEM_NAMES is a generation error that names the action."""

import inspect
from pathlib import Path

import pytest

from apiserver.client_generator.python_renderer import Unsupported, render_service
from apiserver.client_generator.schema_model import load_schema

HERE = Path(__file__).parent
ROOT = HERE.parents[1]  # apiserver/
SESSION = "cairnml.backend_api.session"
EVENT_TYPES = {
    "MetricsScalarEvent": "training_stats_scalar",
    "MetricsVectorEvent": "training_stats_vector",
    "MetricsImageEvent": "training_debug_image",
    "MetricsPlotEvent": "plot",
    "TaskLogEvent": "log",
}


def _events_text() -> str:
    return render_service(load_schema(ROOT / "schema" / "services")["events"], SESSION)


@pytest.fixture(scope="module")
def generated():
    pytest.importorskip("cairnml")
    namespace = {"__name__": "generated_events"}
    exec(compile(_events_text(), "events.py", "exec"), namespace)
    return namespace


def test_the_add_request_is_a_compound_request_whose_item_is_the_event(generated):
    add = generated["AddRequest"]
    assert issubclass(add, generated["CompoundRequest"])
    assert add._item_prop_name == "event"


def test_the_item_is_the_first_argument_of_the_add_request(generated):
    parameters = list(inspect.signature(generated["AddRequest"]).parameters)
    assert parameters[:3] == ["event", "model_event", "allow_locked"]


def test_a_value_that_is_not_an_event_is_refused(generated):
    with pytest.raises(TypeError, match="event"):
        generated["AddRequest"]({"task": "t", "type": "log"})


def test_the_body_is_the_event_merged_with_the_request_options(generated):
    event = generated["MetricsScalarEvent"](task="t", metric="loss", variant="train", value=1.5, iter=3)
    body = generated["AddRequest"](event=event, model_event=True).to_dict()
    assert body["type"] == "training_stats_scalar"
    assert body["task"] == "t"
    assert body["value"] == 1.5
    assert body["iter"] == 3
    assert body["model_event"] is True
    assert "event" not in body


def test_the_compound_request_validates_the_event_against_the_alternatives(generated):
    event = generated["MetricsScalarEvent"](task="t", metric="loss", variant="train", value=1.5, iter=3)
    generated["AddRequest"](event=event).validate()


@pytest.mark.parametrize("class_name", sorted(EVENT_TYPES))
def test_an_event_class_sends_its_type_constant_without_a_constructor_argument(generated, class_name):
    event_class = generated[class_name]
    assert "type" not in inspect.signature(event_class).parameters


@pytest.mark.parametrize("class_name", sorted(EVENT_TYPES))
def test_the_type_constant_is_read_only_and_always_on_the_wire(generated, class_name):
    event_class = generated[class_name]
    event = event_class(task="t")
    assert event.type == EVENT_TYPES[class_name]
    assert event.to_dict()["type"] == EVENT_TYPES[class_name]
    with pytest.raises(AttributeError):
        event.type = "something-else"


def test_a_compound_request_without_an_item_name_is_a_generation_error():
    schema = load_schema(HERE / "fixtures" / "unnamed")
    with pytest.raises(Unsupported, match="widgets.add"):
        render_service(schema["widgets"], SESSION)


def test_the_batch_of_events_takes_the_add_request_unchanged(generated):
    batch = generated["AddBatchRequest"]
    assert issubclass(batch, generated["BatchRequest"])
    assert batch._batched_request_cls is generated["AddRequest"]
