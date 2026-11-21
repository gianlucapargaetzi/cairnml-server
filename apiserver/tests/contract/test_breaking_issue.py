"""The follow-up issue for a breaking API change: when there is one, what it is called, how an earlier one is found, and
what its body says. All of it is decided here, not in shell."""

import json

import pytest

from apiserver.contract import breaking_issue
from apiserver.contract.breaking_issue import (
    build_body,
    build_comment,
    build_title,
    find_issue,
    title_prefix,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
BASE = "fedcba9876543210fedcba9876543210fedcba98"
SECTION = (
    "## What changed in the API\n\nBreaking changes: 2\n\n| Endpoint | Change |\n| --- | --- |\n| `a.b` | removed |\n"
)


def _change(endpoint, breaking=True, kind="changed", detail="response.x: field removed"):
    return {
        "endpoint": endpoint,
        "kind": kind,
        "breaking": breaking,
        "detail": detail,
        "old_def": "2.41",
        "new_def": "2.42",
    }


def _report(changes, top="2.42", base_api="2.41"):
    return {
        "base_ref": BASE,
        "base_api": base_api,
        "head_api": top,
        "top": top,
        "changes": changes,
        "problems": [],
    }


def test_the_title_names_the_version_and_the_endpoints():
    title = build_title(_report([_change("devices.exchange"), _change("tasks.get_all")]))
    assert title == "API breaking change 2.42: devices.exchange, tasks.get_all"
    assert title.startswith(title_prefix("2.42"))


def test_the_title_lists_an_endpoint_once_and_only_the_breaking_ones():
    report = _report(
        [
            _change("login.supported_modes"),
            _change("login.supported_modes", detail="at API 2.1: response.x: field removed"),
            _change("devices.git_token", breaking=False, detail="response.n: field added"),
        ]
    )
    assert build_title(report) == "API breaking change 2.42: login.supported_modes"


def test_the_title_shows_five_endpoints_and_counts_the_rest():
    report = _report([_change(f"devices.e{i}") for i in range(8)])
    assert (
        build_title(report)
        == "API breaking change 2.42: devices.e0, devices.e1, devices.e2, devices.e3, devices.e4 and 3 more"
    )


def test_the_title_never_exceeds_two_hundred_characters():
    report = _report([_change("service" + "x" * 60 + f".action{i}") for i in range(30)])
    title = build_title(report)
    assert len(title) <= 200 and title.startswith("API breaking change 2.42: ") and "more" in title


def test_one_endpoint_name_that_is_too_long_is_cut():
    title = build_title(_report([_change("a" * 500)]))
    assert len(title) <= 200 and title.endswith("...")


def test_the_title_has_no_line_breaks_or_control_characters():
    title = build_title(_report([_change("devices.x\nSecond line\x07")]))
    assert "\n" not in title and "\x07" not in title


@pytest.mark.parametrize("top", ["2.42; rm -rf /", "$(id)", "2", "2.4.2", "", "v2.42", "2.42\n"])
def test_a_version_that_is_not_a_version_is_refused(top):
    with pytest.raises(ValueError):
        build_title(_report([_change("a.b")], top=top))
    with pytest.raises(ValueError):
        title_prefix(top)


def test_a_report_without_breaking_changes_has_no_title():
    with pytest.raises(ValueError):
        build_title(_report([_change("a.b", breaking=False)]))


def _issue(number, title, state="OPEN"):
    return {"number": number, "title": title, "state": state}


def test_an_open_issue_for_the_version_is_found():
    issues = [_issue(3, "API breaking change 2.41: a.b"), _issue(7, "API breaking change 2.42: c.d and 2 more")]
    assert find_issue(issues, "2.42") == ("open", 7)


def test_a_closed_issue_for_the_version_is_found_as_closed():
    assert find_issue([_issue(7, "API breaking change 2.42: c.d", "CLOSED")], "2.42") == ("closed", 7)


def test_an_open_issue_wins_over_a_closed_one():
    issues = [_issue(2, "API breaking change 2.42: a.b", "CLOSED"), _issue(9, "API breaking change 2.42: a.b")]
    assert find_issue(issues, "2.42") == ("open", 9)


def test_the_lowest_open_number_is_chosen_when_there_are_several():
    issues = [_issue(9, "API breaking change 2.42: a.b"), _issue(4, "API breaking change 2.42: a.b")]
    assert find_issue(issues, "2.42") == ("open", 4)


def test_no_issue_for_the_version_means_none():
    issues = [
        _issue(1, "API breaking change 2.4: a.b"),
        _issue(2, "Another API breaking change 2.42: a.b"),
        _issue(3, "x"),
    ]
    assert find_issue(issues, "2.42") == ("none", None)
    assert find_issue([], "2.42") == ("none", None)


def test_the_prefix_ends_at_the_colon_so_that_two_versions_never_match_each_other():
    assert find_issue([_issue(1, "API breaking change 2.4: a.b")], "2.41") == ("none", None)
    assert find_issue([_issue(1, "API breaking change 2.41: a.b")], "2.4") == ("none", None)


def test_the_body_has_the_versions_the_commit_the_changelog_link_the_checklist_and_the_section():
    link = "https://example.invalid/CHANGELOG.md#api-242"
    body = build_body(_report([_change("a.b")]), SECTION, SHA, link, "Base: the dispatch input base_ref.")
    assert "`2.41`" in body and "`2.42`" in body
    assert f"cairnml-server@{SHA}" in body
    assert link in body
    assert body.count("- [ ] ") == 3
    assert "Adopt the new definition" in body and "old definition" in body and "dropped" in body
    assert SECTION.strip() in body
    assert "Base: the dispatch input base_ref." in body


def test_the_body_says_when_there_is_no_changelog_section():
    body = build_body(_report([_change("a.b")]), SECTION, SHA, "no changelog section", "")
    assert "no changelog section" in body


def test_a_body_with_a_huge_section_stays_below_the_size_limit():
    rows = "".join(f"| `a.e{i:04}` | {'x' * 300} |\n" for i in range(3000))
    body = build_body(
        _report([_change("a.b")]),
        "## What changed in the API\n\n| Endpoint | Change |\n| --- | --- |\n" + rows,
        SHA,
        "no changelog section",
        "",
    )
    assert len(body) <= 60000 and "- [ ] " in body


def test_a_sha_that_is_not_a_full_sha_is_refused():
    with pytest.raises(ValueError):
        build_body(_report([_change("a.b")]), SECTION, "main", "no changelog section", "")


def test_the_comment_has_the_section_and_the_commit_but_not_the_checklist():
    comment = build_comment(_report([_change("a.b")]), SECTION, SHA, "Base: the commit before this push.")
    assert SECTION.strip() in comment and f"cairnml-server@{SHA}" in comment
    assert "- [ ] " not in comment


def _write(tmp_path, report, section=SECTION, changelog="## API 2.42\n\n- a.b\n"):
    (tmp_path / "report.json").write_text(json.dumps(report))
    (tmp_path / "section.md").write_text(section)
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return [
        "--json-report",
        str(tmp_path / "report.json"),
        "--section-file",
        str(tmp_path / "section.md"),
        "--changelog",
        str(tmp_path / "CHANGELOG.md"),
        "--sha",
        SHA,
        "--note",
        "Base: test.",
        "--out-dir",
        str(tmp_path / "out"),
    ]


def test_plan_writes_the_files_for_a_breaking_report(tmp_path, capsys):
    args = _write(tmp_path, _report([_change("a.b"), _change("c.d", breaking=False)]))
    assert breaking_issue.main(["plan", *args]) == 0
    assert capsys.readouterr().out.strip() == "breaking 2.42"
    out = tmp_path / "out"
    assert (out / "title.txt").read_text() == "API breaking change 2.42: a.b"
    body = (out / "body.md").read_text()
    assert "/blob/" + SHA + "/CHANGELOG.md#api-242" in body and "- [ ] " in body
    assert "- [ ] " not in (out / "comment.md").read_text()


def test_plan_says_no_changelog_section_when_the_changelog_lacks_the_version(tmp_path):
    args = _write(tmp_path, _report([_change("a.b")]), changelog="## API 2.41\n")
    assert breaking_issue.main(["plan", *args]) == 0
    assert "no changelog section" in (tmp_path / "out" / "body.md").read_text()


@pytest.mark.parametrize(
    "changes",
    [
        [],
        [{"endpoint": "a.b", "kind": "added", "breaking": False, "detail": "new", "old_def": None, "new_def": "2.42"}],
    ],
)
def test_plan_writes_nothing_when_nothing_is_breaking(tmp_path, capsys, changes):
    args = _write(tmp_path, _report(changes))
    assert breaking_issue.main(["plan", *args]) == 0
    assert capsys.readouterr().out.strip() == "none"
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_plan_refuses_a_report_that_is_not_one(tmp_path, capsys):
    args = _write(tmp_path, {"changes": "nope"})
    assert breaking_issue.main(["plan", *args]) == 1
    assert "error:" in capsys.readouterr().err


def test_find_prints_what_to_do_for_a_listing(tmp_path, capsys):
    (tmp_path / "report.json").write_text(json.dumps(_report([_change("a.b")])))
    listing = tmp_path / "issues.json"
    for issues, expected in (
        ([], "none"),
        ([_issue(5, "API breaking change 2.42: a.b")], "open 5"),
        ([_issue(5, "API breaking change 2.42: a.b", "CLOSED")], "closed 5"),
    ):
        listing.write_text(json.dumps(issues))
        assert (
            breaking_issue.main(["find", "--json-report", str(tmp_path / "report.json"), "--issues-file", str(listing)])
            == 0
        )
        assert capsys.readouterr().out.strip() == expected


def test_a_pull_request_is_never_the_issue_of_a_version():
    # the REST endpoint lists pull requests as issues too; they carry a pull_request key
    pull = {"number": 8, "title": "API breaking change 2.42: a.b", "state": "open", "pull_request": {"url": "u"}}
    assert find_issue([pull], "2.42") == ("none", None)
    issue = {"number": 9, "title": "API breaking change 2.42: a.b", "state": "open"}
    assert find_issue([pull, issue], "2.42") == ("open", 9)


def test_the_states_of_the_rest_endpoint_are_lower_case():
    assert find_issue([{"number": 4, "title": "API breaking change 2.42: a.b", "state": "open"}], "2.42") == ("open", 4)
    assert find_issue([{"number": 4, "title": "API breaking change 2.42: a.b", "state": "closed"}], "2.42") == (
        "closed",
        4,
    )


def test_a_listing_of_one_page_is_a_list(tmp_path):
    path = tmp_path / "issues.json"
    path.write_text(json.dumps([{"number": 1, "title": "x", "state": "open"}]))
    assert breaking_issue.load_listing(path) == [{"number": 1, "title": "x", "state": "open"}]


def test_a_paginated_listing_is_the_pages_one_after_the_other(tmp_path):
    # `gh api --paginate` prints one JSON array per page, without a separator
    path = tmp_path / "issues.json"
    path.write_text(json.dumps([{"number": 1}]) + "\n" + json.dumps([{"number": 2}, {"number": 3}]) + "\n[]\n")
    assert [i["number"] for i in breaking_issue.load_listing(path)] == [1, 2, 3]


def test_a_listing_with_an_empty_page_only_is_empty(tmp_path):
    path = tmp_path / "issues.json"
    path.write_text("[]\n")
    assert breaking_issue.load_listing(path) == []


@pytest.mark.parametrize("text", ["", "   \n", "{}", '{"message": "Not Found"}', "[1, 2]", "[] nonsense", "not json"])
def test_a_listing_that_is_not_a_list_of_issues_is_refused(tmp_path, text):
    path = tmp_path / "issues.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        breaking_issue.load_listing(path)


def test_find_reads_the_pages_of_the_rest_endpoint(tmp_path, capsys):
    (tmp_path / "report.json").write_text(json.dumps(_report([_change("a.b")])))
    listing = tmp_path / "issues.json"
    page_one = [{"number": n, "title": f"Issue {n}", "state": "open"} for n in range(1, 101)]
    page_two = [{"number": 101, "title": "API breaking change 2.42: a.b", "state": "closed"}]
    listing.write_text(json.dumps(page_one) + "\n" + json.dumps(page_two) + "\n")
    assert (
        breaking_issue.main(["find", "--json-report", str(tmp_path / "report.json"), "--issues-file", str(listing)])
        == 0
    )
    assert capsys.readouterr().out.strip() == "closed 101"
