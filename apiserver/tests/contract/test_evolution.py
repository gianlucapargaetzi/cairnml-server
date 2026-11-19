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


UNION_BASE = """
lookup {
  "2.41" {
    request { type = object, properties { id { type = string } } }
    response { type = object, properties { project { type = string } } }
  }
}
"""


def _union_changes(tmp_path, head_body):
    base_api = load_api(_write(tmp_path / "base", UNION_BASE))
    head_api = load_api(_write(tmp_path / "head", head_body))
    return diff(base_api, head_api, "2.41", "2.41")


def test_a_string_field_that_becomes_a_string_or_object_is_breaking(tmp_path):
    head = UNION_BASE.replace(
        "project { type = string }",
        "project { anyOf = [ { type = string }, { type = object, properties { id { type = string } } } ] }",
    )
    breaking = [c for c in _union_changes(tmp_path, head) if c.breaking]
    assert breaking and any("project" in c.detail and "became" in c.detail for c in breaking), breaking


def test_a_union_whose_members_change_is_breaking_and_an_identical_union_is_not(tmp_path):
    base_union = UNION_BASE.replace(
        "project { type = string }",
        "project { anyOf = [ { type = string }, { type = object, properties { id { type = string } } } ] }",
    )
    base_api = load_api(_write(tmp_path / "base", base_union))
    same = load_api(_write(tmp_path / "same", base_union))
    assert not [c for c in diff(base_api, same, "2.41", "2.41") if c.breaking]
    wider = base_union.replace(
        "{ type = object, properties { id { type = string } } } ]",
        "{ type = object, properties { id { type = string } } } { type = number } ]",
    )
    changed = load_api(_write(tmp_path / "wider", wider))
    assert [c for c in diff(base_api, changed, "2.41", "2.41") if c.breaking]


def _exchange_with(versions):
    """The exchange action with one definition per version (the reassign action of BASE is kept)."""
    blocks = "".join(f'  "{v}" {{ request {{ type = object }} response {{ type = object }} }}\n' for v in versions)
    return "exchange {\n" + blocks + "}\nreassign {" + BASE.split("reassign {")[1]


def test_a_sixth_definition_of_an_endpoint_is_a_problem(tmp_path):
    from apiserver.contract.evolution import assess

    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", _exchange_with(["2.36", "2.37", "2.38", "2.39", "2.40", "2.41"])))
    _, problems, _ = assess(base, head, "2.41", "2.41", "")
    assert any("exchange keeps 6 definitions; the cap is 5 per endpoint" in problem for problem in problems)


def test_five_definitions_of_an_endpoint_are_allowed(tmp_path):
    from apiserver.contract.evolution import assess

    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", _exchange_with(["2.37", "2.38", "2.39", "2.40", "2.41"])))
    _, problems, _ = assess(base, head, "2.41", "2.41", "")
    assert not any("the cap" in problem for problem in problems)


CHANGED_EXCHANGE = """
exchange {
  "2.41" {
    request { type = object, required = [assertion], properties { assertion { type = string }, extra { type = string } } }
    response { type = object, properties { token { type = string } } }
  }
}
"""


def test_an_edited_definition_at_a_raised_version_needs_a_definition_of_its_own(tmp_path):
    from apiserver.contract.evolution import assess

    head_body = CHANGED_EXCHANGE + BASE.split("exchange {")[0] + "reassign {" + BASE.split("reassign {")[1]
    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", head_body))
    _, problems, top = assess(base, head, "2.41", "2.42", "## API 2.42\n- devices.exchange: changed\n")
    assert top == "2.42"
    assert any("devices.exchange changed at API 2.42 without a definition labelled newer than 2.41" in p for p in problems)


def test_a_new_definition_at_the_raised_version_is_the_way_to_change_an_endpoint(tmp_path):
    from apiserver.contract.evolution import assess

    head_body = BASE.replace('exchange {\n  "2.41" {', 'exchange {\n  "2.42" {\n    request { type = object, required = [assertion], properties { assertion { type = string }, extra { type = string } } }\n    response { type = object, properties { token { type = string } } }\n  }\n  "2.41" {')
    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", head_body))
    _, problems, top = assess(base, head, "2.41", "2.42", "## API 2.42\n- devices.exchange: a field added\n")
    assert top == "2.42" and not any("without a definition" in p for p in problems)


def test_a_breaking_change_inside_an_older_definition_needs_a_changelog_entry(tmp_path):
    from apiserver.contract.evolution import assess

    # the 2.41 definition loses its response field (what a changed shared definition does to every block that uses it);
    # the 2.42 definition is the new one, so the change is made at a raised version
    head_body = """
exchange {
  "2.42" {
    request { type = object, required = [assertion], properties { assertion { type = string } } }
    response { type = object, properties { token { type = string } } }
  }
  "2.41" {
    request { type = object, required = [assertion], properties { assertion { type = string } } }
    response { type = object }
  }
}
""" + "reassign {" + BASE.split("reassign {")[1]
    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", head_body))
    _, unnamed, _ = assess(base, head, "2.41", "2.42", "## API 2.42\n- devices.reassign: x\n")
    assert any("devices.exchange" in p and "at API 2.41" in p and "token" in p for p in unnamed)
    _, named, _ = assess(base, head, "2.41", "2.42", "## API 2.42\n- devices.exchange: token left the 2.41 definition\n")
    assert not any("at API 2.41" in p for p in named)


def test_a_duplicate_definition_in_a_schema_file_stops_the_load(tmp_path):
    body = 'exchange {\n  "2.41" { request { type = object } response { type = object } }\n  "2.41" { request { type = object } response { type = object } }\n}\n'
    with pytest.raises(ValueError, match="two definitions of one version in one action: exchange 2.41"):
        load_api(_write(tmp_path / "d", body))



IN_PLACE_EXTRA = BASE.replace(
    "properties { token { type = string } }",
    "properties { token { type = string }, note { type = string } }",
)


def test_an_additive_change_in_place_without_a_new_definition_fails_and_names_the_endpoint(tmp_path):
    from apiserver.contract.evolution import assess

    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", IN_PLACE_EXTRA))
    _, problems, top = assess(base, head, "2.41", "2.41", "")
    assert top == "2.41"
    assert any(
        "devices.exchange changed at API 2.41 without a definition labelled newer than 2.41" in p for p in problems
    ), problems


def test_the_same_additive_change_with_a_newer_definition_passes(tmp_path):
    from apiserver.contract.evolution import assess

    head_body = IN_PLACE_EXTRA.replace(
        'exchange {\n  "2.41" {',
        'exchange {\n  "2.42" {\n    request { type = object, required = [assertion], properties { assertion { type = string } } }\n'
        '    response { type = object, properties { token { type = string }, note { type = string } } }\n  }\n  "2.41" {',
    )
    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", head_body))
    _, problems, top = assess(base, head, "2.41", "2.42", "## API 2.42\n")
    assert top == "2.42" and problems == [], problems


def test_a_description_or_comment_only_change_in_place_passes(tmp_path):
    from apiserver.contract.evolution import assess

    head_body = BASE.replace(
        "assertion { type = string }",
        'assertion { type = string, description = "the assertion of the device" }',
    ).replace("exchange {", "# a comment that is not part of the contract\nexchange {")
    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", head_body))
    changes, problems, _ = assess(base, head, "2.41", "2.41", "")
    assert changes == [] and problems == [], (changes, problems)


def test_a_changelog_entry_must_name_the_endpoint_exactly(tmp_path):
    head = BASE.replace("assertion { type = string }", "signed_assertion { type = string }").replace(
        "required = [assertion]", "required = [signed_assertion]"
    )
    _, problems = _check(tmp_path, head, head_api="2.42", changelog="## API 2.42\n\n- devices.exchange_all: renamed\n")
    assert problems and "devices.exchange: request.assertion: field removed is not named" in problems[0], problems
    _, problems = _check(tmp_path, head, head_api="2.42", changelog="## API 2.42\n\n- devices.exchange: renamed\n")
    assert problems == []
