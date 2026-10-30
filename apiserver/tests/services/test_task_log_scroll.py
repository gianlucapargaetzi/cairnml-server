import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from apiserver.apierrors import errors
from apiserver.bll.event.event_bll import EventBLL
from apiserver.bll.event.event_common import EventType
from apiserver.bll.event.events_iterator import EventsIterator
from apiserver.bll.event import events_iterator


def response(timestamps):
    """A search response with one log event per timestamp."""
    return {
        "hits": {
            "total": {"value": len(timestamps)},
            "hits": [{"_id": f"e{i}", "_source": {"timestamp": ts, "msg": f"line {i}"}} for i, ts in enumerate(timestamps)],
        },
    }


def event_bll(es):
    bll = object.__new__(EventBLL)
    bll.es = es
    return bll


def legacy_log(bll, batch_size, order="desc", scroll_id=None):
    with patch("apiserver.bll.event.event_bll.check_empty_data", return_value=False):
        return bll.scroll_task_events("company", "task", order, EventType.task_log, batch_size=batch_size, scroll_id=scroll_id)


def test_a_first_page_shorter_than_its_size_ends_the_log_with_no_scroll():
    es = MagicMock()
    es.search.return_value = response([1, 2, 3])
    bll = event_bll(es)

    events, scroll_id, total = legacy_log(bll, batch_size=50)

    assert [ev["timestamp"] for ev in events] == [1, 2, 3]
    assert total == 3
    assert scroll_id == EventBLL.empty_scroll
    assert es.search.call_count == 1


def test_a_full_first_page_returns_a_cursor_after_its_last_timestamp():
    es = MagicMock()
    es.search.side_effect = [response([1, 2, 3, 4]), response([4])]
    bll = event_bll(es)

    events, scroll_id, _ = legacy_log(bll, batch_size=4)

    assert [ev["timestamp"] for ev in events] == [1, 2, 3, 4]
    assert scroll_id.startswith("tlc1.")
    assert json.loads(base64.urlsafe_b64decode(scroll_id[len("tlc1."):])) == {"order": "desc", "after": 4}


def test_the_next_page_searches_after_the_cursor_in_the_cursor_order():
    es = MagicMock()
    es.search.side_effect = [response([9, 8]), response([8])]
    bll = event_bll(es)
    _, scroll_id, _ = legacy_log(bll, batch_size=2, order="desc")
    es.search.reset_mock()
    es.search.side_effect = [response([7, 6]), response([6])]

    events, _, _ = legacy_log(bll, batch_size=2, order="asc", scroll_id=scroll_id)

    body = es.search.call_args_list[0].kwargs["body"]
    assert body["search_after"] == [8]
    assert body["sort"] == {"timestamp": {"order": "desc"}}
    assert [ev["timestamp"] for ev in events] == [7, 6]


def test_no_elasticsearch_scroll_is_opened_or_cleared():
    es = MagicMock()
    es.search.side_effect = [response([1, 2]), response([2])]
    bll = event_bll(es)

    legacy_log(bll, batch_size=2)

    es.scroll.assert_not_called()
    es.clear_scroll.assert_not_called()
    for call in es.search.call_args_list:
        assert "scroll" not in call.kwargs


def test_events_with_the_timestamp_at_the_end_of_a_page_are_read_on_that_page():
    es = MagicMock()
    es.search.side_effect = [response([1, 3, 3, 3]), response([3, 3, 3])]
    bll = event_bll(es)

    events, scroll_id, _ = legacy_log(bll, batch_size=4)

    assert [ev["timestamp"] for ev in events] == [1, 3, 3, 3]
    assert es.search.call_args_list[1].kwargs["body"]["query"]["bool"]["must"][1] == {"term": {"timestamp": 3}}


def test_the_empty_marker_returns_no_events_without_a_search():
    es = MagicMock()
    bll = event_bll(es)

    events, scroll_id, total = legacy_log(bll, batch_size=2, scroll_id=EventBLL.empty_scroll)

    assert (events, scroll_id, total) == ([], EventBLL.empty_scroll, 0)
    es.search.assert_not_called()


def test_a_scroll_id_that_is_not_a_cursor_is_refused():
    bll = event_bll(MagicMock())

    with pytest.raises(errors.bad_request.InvalidScrollId):
        legacy_log(bll, batch_size=2, scroll_id="DXF1ZXJ5QW5kRmV0Y2g=")


def test_a_damaged_cursor_is_refused():
    bll = event_bll(MagicMock())

    with pytest.raises(errors.bad_request.InvalidScrollId):
        legacy_log(bll, batch_size=2, scroll_id="tlc1.!!!")


def test_the_api_2_9_task_log_read_opens_no_scroll():
    es = MagicMock()
    es.search.side_effect = [response([1, 2, 3, 4, 5]), response([5])]
    iterator = EventsIterator(es=es)

    with patch.object(events_iterator, "check_empty_data", return_value=False):
        result = iterator.get_task_events(
            event_type=EventType.task_log, company_id="company", task_id="task", batch_size=5, navigate_earlier=True,
        )

    assert len(result.events) == 5
    assert es.search.call_count == 2
    for call in es.search.call_args_list:
        assert "scroll" not in call.kwargs
    es.scroll.assert_not_called()
