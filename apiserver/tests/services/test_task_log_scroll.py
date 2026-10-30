from unittest.mock import MagicMock, patch

from apiserver.bll.event import events_iterator
from apiserver.bll.event.event_bll import EventBLL, TASK_LOG_SCROLL
from apiserver.bll.event.event_common import EventType
from apiserver.bll.event.events_iterator import EventsIterator


def hits(count):
    return {
        "_scroll_id": "scroll-1",
        "hits": {
            "total": {"value": count},
            "hits": [{"_id": f"e{i}", "_source": {"timestamp": i, "msg": f"line {i}"}} for i in range(count)],
        },
    }


def event_bll(es):
    bll = object.__new__(EventBLL)
    bll.es = es
    return bll


def legacy_log(bll, batch_size):
    with patch("apiserver.bll.event.event_bll.check_empty_data", return_value=False):
        return bll.scroll_task_events("company", "task", "desc", EventType.task_log, batch_size=batch_size)


def test_a_first_page_shorter_than_its_size_releases_the_scroll_at_once():
    es = MagicMock()
    es.search.return_value = hits(3)
    bll = event_bll(es)

    events, scroll_id, total = legacy_log(bll, batch_size=50)

    assert len(events) == 3
    assert total == 3
    assert scroll_id == EventBLL.empty_scroll
    es.clear_scroll.assert_called_once_with(scroll_id="scroll-1")


def test_a_full_first_page_keeps_a_short_scroll_for_the_next_page():
    es = MagicMock()
    es.search.return_value = hits(50)
    bll = event_bll(es)

    events, scroll_id, _ = legacy_log(bll, batch_size=50)

    assert len(events) == 50
    assert scroll_id == "scroll-1"
    es.clear_scroll.assert_not_called()
    assert es.search.call_args.kwargs["scroll"] == TASK_LOG_SCROLL


def test_the_api_2_9_task_log_read_opens_no_scroll():
    es = MagicMock()
    es.search.side_effect = [hits(5), hits(1)]
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
