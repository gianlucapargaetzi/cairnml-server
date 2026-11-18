"""The legacy execution.parameters input of a request below API 2.9 is converted to hyperparams on write (#235).

The conversion follows the rules of pre-2.41: the parameters go to the default section (Args) unless named in the
TF_DEFINE section, an explicit hyperparams field of the request is kept, a request without hyperparams takes the
hyperparams of the previous task minus the legacy params it had, and a request from 2.9 on is left alone.
"""

from types import SimpleNamespace

from apiserver.services import tasks as tasks_service
from apiserver.utilities.partial_version import PartialVersion


class _PreviousTask:
    def __init__(self, hyperparams):
        self.hyperparams = hyperparams

    def to_proper_dict(self):
        return {"hyperparams": self.hyperparams}


def _call(requested: str, actual: str | None = None):
    return SimpleNamespace(
        endpoint_name="tasks.create",
        requested_endpoint_version=PartialVersion(requested),
        actual_endpoint_version=PartialVersion(actual or requested),
    )


def _legacy(name, value, section="Args"):
    return {"name": name, "type": "legacy", "value": value, "section": section}


def test_legacy_parameters_go_to_the_default_section_as_hyperparams_below_2_9():
    fields = {"execution": {"parameters": {"a": 1, "TF_DEFINE/b": "x"}}}

    tasks_service.prepare_for_save(_call("2.1"), fields)

    assert fields["hyperparams"] == {
        "Args": {"a": _legacy("a", "1")},
        "TF_DEFINE": {"b": _legacy("b", "x", section="TF_DEFINE")},
    }
    assert "parameters" not in fields["execution"]


def test_an_explicit_hyperparams_field_is_not_replaced_by_the_previous_task_below_2_9():
    fields = {
        "hyperparams": {"Args": {"c": {"name": "c", "type": "float", "value": "2", "section": "Args"}}},
        "execution": {"parameters": {"a": 1}},
    }
    previous = _PreviousTask({"Args": {"old": {"name": "old", "type": "float", "value": "9", "section": "Args"}}})

    tasks_service.prepare_for_save(_call("2.1"), fields, previous_task=previous)

    assert set(fields["hyperparams"]["Args"]) == {"c", "a"}
    assert fields["hyperparams"]["Args"]["a"] == _legacy("a", "1")


def test_the_previous_task_value_is_kept_without_its_legacy_params_below_2_9():
    fields = {"execution": {"parameters": {"a": 1}}}
    previous = _PreviousTask(
        {
            "Args": {
                "keep": {"name": "keep", "type": "float", "value": "2", "section": "Args"},
                "old": _legacy("old", "9"),
            }
        }
    )

    tasks_service.prepare_for_save(_call("2.1"), fields, previous_task=previous)

    assert fields["hyperparams"] == {
        "Args": {
            "keep": {"name": "keep", "type": "float", "value": "2", "section": "Args"},
            "a": _legacy("a", "1"),
        }
    }


def test_the_legacy_parameters_are_judged_by_the_requested_version():
    fields = {"execution": {"parameters": {"a": 1}}}

    tasks_service.prepare_for_save(_call("2.5", actual="2.1"), fields)

    assert fields["hyperparams"] == {"Args": {"a": _legacy("a", "1")}}


def test_the_legacy_parameters_are_left_alone_from_2_9_on():
    fields = {"execution": {"parameters": {"a": 1}}}

    tasks_service.prepare_for_save(_call("2.9"), fields)

    assert "hyperparams" not in fields
    assert fields["execution"]["parameters"] == {"a": 1}
