"""The 2.3 tag rules and the 2.16 metadata rule of bll/compat.py, each called below, at and above its threshold.

The version passed is the requested endpoint version. The calls use a stub call, so no database is needed.
"""

from types import SimpleNamespace

import pytest

from apiserver.bll.compat import conform_output_tags, conform_tags, unescape_metadata
from apiserver.utilities.partial_version import PartialVersion


def _call(version: str, endpoint_name: str = "tasks.get_all"):
    return SimpleNamespace(
        endpoint_name=endpoint_name,
        requested_endpoint_version=PartialVersion(version),
        actual_endpoint_version=PartialVersion(version),
    )


@pytest.mark.parametrize(
    "requested,expected_tags",
    [
        ("2.2", ["a", "b", "z"]),
        ("2.3", ["a", "b"]),
        ("2.4", ["a", "b"]),
    ],
)
def test_output_system_tags_are_merged_into_tags_only_below_2_3(requested, expected_tags):
    doc = {"tags": ["b", "a"], "system_tags": ["z"]}

    conform_output_tags(_call(requested), doc)

    assert doc["tags"] == expected_tags
    assert doc["system_tags"] == ["z"]


@pytest.mark.parametrize("requested", ["2.2", "2.3", "2.4"])
def test_output_tags_are_sorted_at_every_version(requested):
    doc = {"tags": ["c", "a", "b"]}

    conform_output_tags(_call(requested), doc)

    assert doc["tags"] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("2.2", (["mine"], ["development"])),
        ("2.3", (["development", "mine"], None)),
        ("2.4", (["development", "mine"], None)),
    ],
)
def test_input_tags_are_split_into_system_tags_only_below_2_3(requested, expected):
    tags, system_tags = conform_tags(_call(requested), ["development", "mine"], None)

    assert (list(tags), system_tags and list(system_tags)) == expected


@pytest.mark.parametrize("requested", ["2.2", "2.3", "2.4"])
def test_input_tags_with_system_tags_are_never_split(requested):
    tags, system_tags = conform_tags(_call(requested), ["a"], ["s"])

    assert list(tags) == ["a"]
    assert list(system_tags) == ["s"]


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("2.16", []),
        ("2.17", {"k.1": {"key": "k%2E1"}}),
    ],
)
def test_metadata_is_cleared_up_to_2_16_and_unescaped_above(requested, expected):
    doc = {"metadata": {"k%2E1": {"key": "k%2E1"}}}

    unescape_metadata(_call(requested, endpoint_name="models.get_by_id"), doc)

    assert doc["metadata"] == expected
