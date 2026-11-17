"""The API change check: which changes are breaking, and when a breaking change may be merged."""

from pathlib import Path

import pytest

from apiserver.contract.evolution import diff, evaluate, load_api

CHANGELOG_OK = "## API 2.42\n\n- devices.exchange: the field is renamed\n- devices.reassign: removed\n"


def _write(folder: Path, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "devices.conf").write_text(body)
    return folder


BASE = """
exchange {
  "2.41" {
    request { type = object, required = [assertion], properties { assertion { type = string } } }
    response { type = object, properties { token { type = string } } }
  }
}
reassign {
  "2.41" {
    request { type = object, properties { task { type = string } } }
    response { type = object, properties { ok { type = boolean } } }
  }
}
"""


def _check(tmp_path, head_body, base_api="2.41", head_api="2.41", changelog=""):
    base = load_api(_write(tmp_path / "base", BASE))
    head = load_api(_write(tmp_path / "head", head_body))
    changes = diff(base, head, base_api, head_api)
    return changes, evaluate(changes, base_api, head_api, changelog)


def test_identical_schemas_have_no_changes(tmp_path):
    changes, problems = _check(tmp_path, BASE)
    assert changes == [] and problems == []


def test_a_new_endpoint_is_additive_and_never_blocks(tmp_path):
    head = BASE + '\nlist_sessions { "2.42" { request { type = object } response { type = object } } }\n'
    changes, problems = _check(tmp_path, head, head_api="2.42", changelog="## API 2.42\n")
    assert [(c.endpoint, c.kind, c.breaking) for c in changes] == [("devices.list_sessions", "added", False)]
    assert problems == []


def test_a_new_optional_field_is_additive(tmp_path):
    head = BASE.replace(
        "properties { task { type = string } }",
        "properties { task { type = string } note { type = string } }",
    ).replace(
        'reassign {\n  "2.41" {',
        'reassign {\n  "2.42" {\n    request { type = object, properties { task { type = string } note { type = string } } }\n'
        '    response { type = object, properties { ok { type = boolean } } }\n  }\n  "2.41" {',
    )
    changes, problems = _check(tmp_path, head, head_api="2.42")
    reassign = [c for c in changes if c.endpoint == "devices.reassign"]
    assert [(c.kind, c.breaking) for c in reassign] == [("changed", False)], changes
    assert problems == []


def test_removing_an_endpoint_is_breaking_and_needs_a_raised_version_and_a_changelog(tmp_path):
    head = BASE.split("reassign {")[0] + 'reassign {\n  "2.42" {\n    removed = true\n  }\n}\n'
    changes, _ = _check(tmp_path, head, head_api="2.42")
    removed = [c for c in changes if c.endpoint == "devices.reassign"]
    assert [(c.kind, c.breaking) for c in removed] == [("removed", True)]

    _, problems = _check(tmp_path, head, head_api="2.41")
    assert problems and "still 2.41" in problems[0]

    _, problems = _check(tmp_path, head, head_api="2.42", changelog="## API 2.42\n\n- nothing about it\n")
    assert problems and "devices.reassign" in problems[0]

    _, problems = _check(tmp_path, head, head_api="2.42", changelog=CHANGELOG_OK)
    assert problems == []


def test_a_renamed_request_field_is_breaking_and_needs_the_changelog(tmp_path):
    head = BASE.replace(
        'exchange {\n  "2.41" {',
        'exchange {\n  "2.42" {\n'
        "    request { type = object, required = [signed_assertion], properties { signed_assertion { type = string } } }\n"
        "    response { type = object, properties { token { type = string } } }\n"
        '  }\n  "2.41" {',
    )
    changes, problems = _check(tmp_path, head, head_api="2.42")
    breaking = [c for c in changes if c.breaking and c.endpoint == "devices.exchange"]
    assert breaking, changes
    assert any("assertion" in c.detail for c in breaking)
    assert problems and "changelog" in problems[0]
    _, problems = _check(tmp_path, head, head_api="2.42", changelog=CHANGELOG_OK)
    assert problems == []


def test_editing_the_current_version_in_place_is_breaking_without_a_raised_version(tmp_path):
    head = BASE.replace("properties { token { type = string } }", "properties { session { type = string } }")
    _, problems = _check(tmp_path, head, head_api="2.41")
    assert problems and "devices.exchange" in problems[0]


def test_a_block_that_repeats_the_one_in_effect_is_no_change(tmp_path):
    repeated = BASE.replace(
        'exchange {\n  "2.41" {',
        'exchange {\n  "2.42" {\n'
        "    request { type = object, required = [assertion], properties { assertion { type = string } } }\n"
        '    response { type = object, properties { token { type = string } } }\n  }\n  "2.41" {',
    )
    changes, problems = _check(tmp_path, repeated, head_api="2.42")
    assert changes == []
    assert problems == []


def test_a_changelog_section_is_read_up_to_the_next_section(tmp_path):
    from apiserver.contract.evolution import _changelog_section

    section = _changelog_section("## API 2.41\n\n- x\n\n## API 2.42\n\n- devices.reassign\n", "2.42")
    assert "devices.reassign" in section and "- x" not in section


@pytest.mark.parametrize("head_api", ["2.41", "2.40"])
def test_a_lower_or_equal_version_never_allows_a_breaking_change(tmp_path, head_api):
    head = BASE.split("reassign {")[0] + 'reassign {\n  "2.42" {\n    removed = true\n  }\n}\n'
    _, problems = _check(tmp_path, head, head_api=head_api, changelog=CHANGELOG_OK)
    assert problems


def test_a_declared_version_above_the_constant_is_a_problem(tmp_path):
    from apiserver.contract.evolution import assess

    same = "request { type = object, properties { task { type = string } } }\n    response { type = object, properties { ok { type = boolean } } }"
    head = BASE.replace("reassign {", 'reassign {\n  "2.42" {\n    ' + same + "\n  }\n")
    base, headed = load_api(_write(tmp_path / "b", BASE)), load_api(_write(tmp_path / "h", head))
    _, problems, top = assess(base, headed, "2.41", "2.41", "")
    assert top == "2.42"
    assert problems and "raise both together" in problems[0]
    _, problems, top = assess(base, headed, "2.41", "2.42", "## API 2.42\n")
    assert top == "2.42" and problems == []


SHARED = """
_definitions {
  thing {
    type = object
    properties {
      a { type = string }
      b { type = string }
    }
  }
}
lookup {
  "2.41" {
    request { type = object, properties { id { type = string } } }
    response { "$ref" = "#/definitions/thing" }
  }
}
"""


def test_a_field_removed_from_a_shared_definition_is_breaking_even_though_the_endpoint_block_is_the_same(tmp_path):
    head = SHARED.replace("      b { type = string }\n", "")
    base_api = load_api(_write(tmp_path / "base", SHARED))
    head_api = load_api(_write(tmp_path / "head", head))
    changes = diff(base_api, head_api, "2.41", "2.41")
    breaking = [c for c in changes if c.breaking and c.endpoint == "devices.lookup"]
    assert breaking and any("b: field removed" in c.detail for c in breaking), changes
    assert evaluate(changes, "2.41", "2.41", "") != []


def test_a_field_added_to_a_shared_definition_is_additive(tmp_path):
    head = SHARED.replace("      b { type = string }\n", "      b { type = string }\n      c { type = string }\n")
    base_api = load_api(_write(tmp_path / "base", SHARED))
    head_api = load_api(_write(tmp_path / "head", head))
    changes = diff(base_api, head_api, "2.41", "2.41")
    assert [c for c in changes if c.endpoint == "devices.lookup"] and not [c for c in changes if c.breaking]
