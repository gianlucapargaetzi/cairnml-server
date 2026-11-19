"""The version-gated task field rules of bll/task/compat.py, each called below, at and above its threshold.

The version passed is the requested endpoint version: the rules decide by what the client asked for (PR #236).
"""

import pytest

from apiserver.bll.task.compat import legacy_params_copied_back_on_read, legacy_params_on_write
from apiserver.utilities.partial_version import PartialVersion


def _legacy(name, value, section="Args"):
    return {"name": name, "type": "legacy", "value": value, "section": section}


@pytest.mark.parametrize(
    "requested, expected_hyperparams",
    [
        ("2.8", {"Args": {"a": _legacy("a", "1")}}),
        ("2.9", None),
        ("2.10", None),
        ("2.42", None),
    ],
)
def test_legacy_params_on_write_decides_by_the_requested_version(requested, expected_hyperparams):
    fields = {"execution": {"parameters": {"a": 1}}}

    legacy_params_on_write(PartialVersion(requested), fields)

    if expected_hyperparams is None:
        assert "hyperparams" not in fields
        assert fields["execution"]["parameters"] == {"a": 1}
    else:
        assert fields["hyperparams"] == expected_hyperparams
        assert "parameters" not in fields["execution"]


@pytest.mark.parametrize(
    "requested, copied_back",
    [
        ("2.8", True),
        ("2.9", False),
        ("2.10", False),
        ("2.42", False),
    ],
)
def test_legacy_params_are_copied_back_on_read_only_below_2_9(requested, copied_back):
    assert legacy_params_copied_back_on_read(PartialVersion(requested)) is copied_back
