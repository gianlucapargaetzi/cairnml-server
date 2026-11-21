"""The change report of the contract tool: markdown for pull request bodies, JSON for other tools, and the errors that
stop the report instead of publishing an empty one."""

import json
import subprocess

import pytest

from apiserver.contract import evolution
from apiserver.contract.evolution import Change, assess, changelog_link, diff, load_api, render_markdown
from apiserver.tests.contract.test_evolution import BASE, _write

SHA = "0123456789abcdef0123456789abcdef01234567"

ADDITIVE_HEAD = BASE.replace(
    'reassign {\n  "2.41" {',
    'reassign {\n  "2.42" {\n    request { type = object, properties { task { type = string } note { type = string } } }\n'
    '    response { type = object, properties { ok { type = boolean } } }\n  }\n  "2.41" {',
)
BREAKING_HEAD = BASE.replace(
    'exchange {\n  "2.41" {',
    'exchange {\n  "2.42" {\n'
    "    request { type = object, required = [signed_assertion], properties { signed_assertion { type = string } } }\n"
    "    response { type = object, properties { token { type = string } } }\n"
    '  }\n  "2.41" {',
)
REMOVED_HEAD = BASE.split("reassign {")[0] + 'reassign {\n  "2.42" {\n    removed = true\n  }\n}\n'


def _changes(tmp_path, head_body, base_api="2.41", head_api="2.42"):
    base = load_api(_write(tmp_path / "base", BASE))
    head = load_api(_write(tmp_path / "head", head_body))
    return diff(base, head, base_api, head_api)


def _rows(markdown):
    return [line for line in markdown.splitlines() if line.startswith("| ") and "---" not in line][1:]


def test_markdown_lists_an_additive_change_with_its_versions(tmp_path):
    changes = _changes(tmp_path, ADDITIVE_HEAD)
    text = render_markdown(changes, [], "2.41", "2.42", SHA)
    assert text.startswith("## What changed in the API")
    rows = _rows(text)
    assert len(rows) == 1
    assert "`devices.reassign`" in rows[0] and "changed" in rows[0] and "additive" in rows[0]
    assert "request.note: field added" in rows[0]
    assert "2.41" in rows[0] and "2.42" in rows[0]
    assert "Breaking changes: none" in text and "**breaking**" not in text


def test_markdown_marks_a_breaking_change_and_counts_them(tmp_path):
    changes = _changes(tmp_path, BREAKING_HEAD)
    text = render_markdown(changes, [], "2.41", "2.42", SHA)
    breaking_rows = [row for row in _rows(text) if "**breaking**" in row]
    assert breaking_rows and all("`devices.exchange`" in row for row in breaking_rows)
    assert f"Breaking changes: {sum(c.breaking for c in changes)}" in text


def test_markdown_lists_a_removed_endpoint_as_breaking(tmp_path):
    text = render_markdown(_changes(tmp_path, REMOVED_HEAD), [], "2.41", "2.42", SHA)
    rows = _rows(text)
    assert len(rows) == 1 and "`devices.reassign`" in rows[0]
    assert "removed" in rows[0] and "**breaking**" in rows[0]


def test_markdown_for_no_change_says_so_and_has_no_table():
    text = render_markdown([], [], "2.41", "2.41", SHA)
    assert "The API contract did not change" in text
    assert "| Endpoint" not in text


def test_markdown_shows_the_base_and_the_api_versions():
    text = render_markdown([], [], "2.41", "2.42", SHA, head_api="2.41")
    assert "`2.41`" in text and "`2.42`" in text and SHA[:8] in text
    assert "API version of the code" in text and "`2.41`" in text


def test_markdown_lists_the_problems_of_the_check():
    text = render_markdown(
        [],
        ["devices.exchange: the settings changed (breaking, but the API version is still 2.41)"],
        "2.41",
        "2.41",
        SHA,
    )
    assert "Problems found by the contract check" in text
    assert "- `devices.exchange: the settings changed (breaking, but the API version is still 2.41)`" in text


def test_markdown_makes_hostile_text_safe():
    change = Change(
        "devices.x`|y", "changed", True, "value 'a`b|c\n@octocat #12 <b>' is no longer allowed", "2.41", "2.42"
    )
    text = render_markdown([change], [], "2.41", "2.42", SHA)
    rows = _rows(text)
    assert len(rows) == 1 and "\n" not in rows[0]
    # one cell separator per column: the pipes of the text are escaped, the backticks of the text are gone
    assert rows[0].replace("\\|", "").count("|") == 6
    assert "`a`b" not in rows[0] and "@octocat" in rows[0]
    assert "`value " in rows[0] and "@octocat #12 <b>' is no longer allowed`" in rows[0]  # inside one code span


def test_markdown_shows_at_most_forty_rows_and_says_how_many_are_left():
    changes = [
        Change(f"devices.e{i:02}", "changed", False, "request.x: field added", "2.41", "2.42") for i in range(45)
    ]
    text = render_markdown(changes, [], "2.41", "2.42", SHA)
    assert len(_rows(text)) == 40
    assert "and 5 more" in text
    assert "devices.e39" in text and "devices.e40" not in text


def test_markdown_cuts_a_very_long_detail():
    change = Change("devices.x", "changed", True, "d" * 5000, "2.41", "2.42")
    assert len(render_markdown([change], [], "2.41", "2.42", SHA)) < 1500


def test_markdown_is_the_same_for_the_same_input(tmp_path):
    changes = _changes(tmp_path, BREAKING_HEAD)
    assert render_markdown(changes, [], "2.41", "2.42", SHA) == render_markdown(changes, [], "2.41", "2.42", SHA)


def test_changelog_link_points_at_the_section_of_the_version():
    link = changelog_link(SHA, "2.42", "# Changes\n\n## API 2.42\n\n- x\n")
    assert link == f"https://github.com/gianlucapargaetzi/cairnml-server/blob/{SHA}/CHANGELOG.md#api-242"


def test_changelog_link_says_so_when_the_section_is_missing():
    assert changelog_link(SHA, "2.43", "## API 2.42\n") == "no changelog section"
    assert changelog_link(None, "2.42", "## API 2.42\n") == "no changelog section"


def test_the_markdown_names_the_changelog_section_when_given_a_link(tmp_path):
    link = changelog_link(SHA, "2.42", "## API 2.42\n")
    assert f"[CHANGELOG]({link})" in render_markdown([], [], "2.41", "2.42", SHA, changelog_link=link)
    assert "no changelog section" in render_markdown([], [], "2.41", "2.42", SHA, changelog_link="no changelog section")


def test_a_change_knows_the_definitions_it_goes_from_and_to(tmp_path):
    changes = _changes(tmp_path, ADDITIVE_HEAD)
    assert [(c.old_def, c.new_def) for c in changes] == [("2.41", "2.42")]


def test_an_added_endpoint_has_no_old_definition(tmp_path):
    head = BASE + '\nlist_sessions { "2.42" { request { type = object } response { type = object } } }\n'
    added = [c for c in _changes(tmp_path, head) if c.kind == "added"]
    assert [(c.old_def, c.new_def) for c in added] == [(None, "2.42")]


def test_a_change_inside_an_older_definition_names_that_definition_on_both_sides(tmp_path):
    head_body = (
        """
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
"""
        + "reassign {"
        + BASE.split("reassign {")[1]
    )
    older = [c for c in _changes(tmp_path, head_body) if "at API 2.41" in c.detail]
    assert older and all((c.old_def, c.new_def) == ("2.41", "2.41") for c in older)


def _fake_refs(monkeypatch, tmp_path, base_body=BASE, base_api="2.41"):
    base_dir = _write(tmp_path / "from-ref", base_body)

    def fake(ref, destination):
        return base_dir, base_api

    monkeypatch.setattr(evolution, "_schema_from_ref", fake)


def _run(tmp_path, head_body, extra=(), head_api="2.41"):
    head_dir = _write(tmp_path / "head-schema", head_body)
    return evolution.main(
        [
            "--base-ref",
            SHA,
            "--head-schema",
            str(head_dir),
            "--head-api",
            head_api,
            "--head-sha",
            SHA,
            "--changelog",
            str(tmp_path / "no-changelog.md"),
            *extra,
        ]
    )


def test_the_reports_are_written_even_when_problems_make_the_exit_code_one(tmp_path, monkeypatch, capsys):
    _fake_refs(monkeypatch, tmp_path)
    md, js = tmp_path / "report.md", tmp_path / "report.json"
    code = _run(tmp_path, BREAKING_HEAD, ["--markdown-report", str(md), "--json-report", str(js)], head_api="2.41")
    assert code == 1
    assert "PROBLEM" in capsys.readouterr().out  # the text output is unchanged
    assert "## What changed in the API" in md.read_text() and "Problems found by the contract check" in md.read_text()
    report = json.loads(js.read_text())
    assert set(report) == {"base_ref", "base_api", "head_api", "top", "changes", "problems"}
    assert report["base_ref"] == SHA and report["base_api"] == "2.41" and report["head_api"] == "2.41"
    assert report["top"] == "2.42" and report["problems"]
    assert {"endpoint", "kind", "breaking", "detail", "old_def", "new_def"} == set(report["changes"][0])


def test_the_text_output_and_the_exit_code_are_unchanged_without_report_options(tmp_path, monkeypatch, capsys):
    _fake_refs(monkeypatch, tmp_path)
    assert _run(tmp_path, ADDITIVE_HEAD, head_api="2.42") == 0
    out = capsys.readouterr().out
    assert out.startswith("API 2.41 -> 2.42 (API version 2.42), compared with ") and out.rstrip().endswith(
        "0 problem(s)"
    )
    assert not list(tmp_path.glob("*.md")) and not list(tmp_path.glob("*.json"))


def test_a_clean_run_writes_the_no_change_text(tmp_path, monkeypatch):
    _fake_refs(monkeypatch, tmp_path)
    md = tmp_path / "report.md"
    assert _run(tmp_path, BASE, ["--markdown-report", str(md)]) == 0
    assert "The API contract did not change" in md.read_text()


def test_a_ref_that_cannot_be_read_writes_no_report_and_names_the_reason(tmp_path, capsys):
    md = tmp_path / "report.md"
    head_dir = _write(tmp_path / "head-schema", BASE)
    code = evolution.main(
        [
            "--base-ref",
            "no-such-ref-328",
            "--head-schema",
            str(head_dir),
            "--head-api",
            "2.41",
            "--markdown-report",
            str(md),
        ]
    )
    assert code == 1 and not md.exists()
    err = capsys.readouterr().err
    assert "no-such-ref-328" in err and "error:" in err


def test_schema_from_ref_raises_a_value_error_with_the_stderr_of_git(tmp_path):
    with pytest.raises(ValueError) as error:
        evolution._schema_from_ref("no-such-ref-328", tmp_path)
    assert "no-such-ref-328" in str(error.value)
    assert not isinstance(error.value, subprocess.CalledProcessError)


def test_a_broken_schema_stops_the_report(tmp_path, monkeypatch, capsys):
    _fake_refs(monkeypatch, tmp_path)
    md = tmp_path / "report.md"
    code = _run(tmp_path, BASE + "\nexchange {\n", ["--markdown-report", str(md)])
    assert code != 0 and not md.exists()


def test_assess_still_returns_the_changes_problems_and_top(tmp_path):
    base = load_api(_write(tmp_path / "b", BASE))
    head = load_api(_write(tmp_path / "h", ADDITIVE_HEAD))
    changes, problems, top = assess(base, head, "2.41", "2.42", "")
    assert top == "2.42" and problems == [] and len(changes) == 1
