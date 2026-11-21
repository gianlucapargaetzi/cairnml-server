"""The body of a regeneration pull request: one base marker, one cumulative section, a size below GitHub's limit."""

import pytest

from apiserver.contract import pr_body
from apiserver.contract.pr_body import BODY_LIMIT, build_body, read_marker, read_note

BASE = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
SECTION = (
    "## What changed in the API\n\nBreaking changes: none\n\n| Endpoint | Change |\n| --- | --- |\n| `a` | changed |\n"
)


def test_the_marker_is_read_from_a_body_it_was_written_into():
    body = build_body("intro", SECTION, BASE)
    assert read_marker(body) == BASE


def test_a_body_without_a_marker_has_no_base():
    assert read_marker("The schema of cairnml-server changed.") is None
    assert read_marker("") is None


def _marker(value):
    # built from parts so that no line of this file looks like a key with a high-entropy value to the secret scanner
    return "<!-- api-sync-base:" + value + " -->"


@pytest.mark.parametrize(
    "line",
    [
        _marker(BASE.upper()),
        _marker(BASE[:39]),  # 39 characters
        _marker(BASE + "8"),  # 41 characters
        _marker("$(touch /tmp/x)" + BASE[:25]),
        _marker(BASE + "; rm -rf /"),
        _marker(""),
        "text " + _marker(BASE) + " text",  # not a line of its own
        "> " + _marker(BASE),
    ],
)
def test_a_forged_marker_is_ignored(line):
    assert read_marker(f"intro\n\n{line}\n\n{SECTION}") is None


def test_the_first_marker_line_counts_and_a_later_forged_one_does_not_replace_it():
    body = f"{_marker(BASE)}\ntext\n{_marker(OTHER)}\n"
    assert read_marker(body) == BASE
    broken = f"{_marker('nonsense')}\n{_marker(OTHER)}\n"
    assert read_marker(broken) is None


def test_the_body_has_the_intro_one_marker_and_the_section():
    body = build_body("The intro.", SECTION, BASE)
    assert body.startswith("The intro.")
    assert body.count("<!-- api-sync-base:") == 1
    assert SECTION.strip() in body


def test_the_same_inputs_give_the_same_body():
    assert build_body("i", SECTION, BASE, "note") == build_body("i", SECTION, BASE, "note")


def test_the_note_says_which_base_was_used():
    body = build_body("i", SECTION, BASE, "the base is the first parent: the previous commit was not in the history")
    assert "the first parent" in body and BASE[:8] in body


def test_a_body_without_a_marker_is_possible_for_an_issue():
    body = build_body("The findings.", SECTION, None)
    assert "api-sync-base" not in body and SECTION.strip() in body


def test_a_base_that_is_not_a_full_sha_is_refused():
    for bad in ("main", BASE.upper(), BASE[:8], BASE + "0", "$(x)"):
        with pytest.raises(ValueError):
            build_body("i", SECTION, bad)


def _long_section(rows, width=300):
    table = "".join(f"| `devices.e{i:03}` | changed | {'x' * width} |\n" for i in range(rows))
    return (
        "## What changed in the API\n\nBreaking changes: 3\n\n| Endpoint | Change | Detail |\n| --- | --- | --- |\n"
        + table
    )


def test_a_body_over_the_limit_loses_table_rows_before_anything_else():
    body = build_body("The intro.", _long_section(400), BASE)
    assert len(body) <= BODY_LIMIT < 65536
    assert body.startswith("The intro.") and read_marker(body) == BASE
    assert "Breaking changes: 3" in body
    assert "devices.e000" in body and "devices.e399" not in body
    assert "left out to fit the size limit" in body


def test_a_body_under_the_limit_is_not_changed():
    section = _long_section(20)
    assert section.strip() in build_body("i", section, BASE)
    assert "left out" not in build_body("i", section, BASE)


def test_a_section_without_rows_that_is_too_long_is_cut_not_refused():
    body = build_body("The intro.", "## What changed in the API\n\n" + "problem " * 20000, BASE)
    assert len(body) <= BODY_LIMIT and read_marker(body) == BASE and "truncated" in body


def test_the_command_line_reads_the_marker_from_a_file(tmp_path, capsys):
    path = tmp_path / "body.md"
    path.write_text(build_body("i", SECTION, BASE))
    assert pr_body.main(["read-marker", str(path)]) == 0
    assert capsys.readouterr().out.strip() == BASE


def test_the_command_line_prints_nothing_for_a_forged_marker(tmp_path, capsys):
    path = tmp_path / "body.md"
    path.write_text(_marker("$(id)") + "\n")
    assert pr_body.main(["read-marker", str(path)]) == 0
    assert capsys.readouterr().out == ""


def test_the_command_line_builds_a_body_from_files(tmp_path):
    (tmp_path / "intro.md").write_text("The intro.\n")
    (tmp_path / "section.md").write_text(SECTION)
    out = tmp_path / "body.md"
    assert (
        pr_body.main(
            [
                "build",
                "--intro-file",
                str(tmp_path / "intro.md"),
                "--section-file",
                str(tmp_path / "section.md"),
                "--base",
                BASE,
                "--note",
                "a note",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert read_marker(out.read_text()) == BASE and "a note" in out.read_text()


def test_the_command_line_refuses_a_bad_base(tmp_path, capsys):
    (tmp_path / "intro.md").write_text("i")
    (tmp_path / "section.md").write_text(SECTION)
    code = pr_body.main(
        [
            "build",
            "--intro-file",
            str(tmp_path / "intro.md"),
            "--section-file",
            str(tmp_path / "section.md"),
            "--base",
            "main",
            "--out",
            str(tmp_path / "b.md"),
        ]
    )
    assert code == 1 and not (tmp_path / "b.md").exists()


def test_the_reason_for_the_base_is_read_back_so_that_a_rerun_writes_the_same_text():
    note = "Base: the first parent, because the commit before this push is not an ancestor of it."
    body = build_body("i", SECTION, BASE, note)
    assert read_note(body) == note
    assert build_body("i", SECTION, read_marker(body), read_note(body)) == body


def test_the_reason_for_the_base_of_a_dispatch_with_an_underscore_is_read_back():
    body = build_body("i", SECTION, BASE, "Base: the dispatch input base_ref.")
    assert read_note(body) == "Base: the dispatch input base_ref."


def test_a_body_without_a_valid_marker_has_no_reason():
    assert read_note("text\n_Compared from 01234567. Base: x._\n") is None
    forged = _marker("nonsense") + "\n_Compared from 01234567. Base: x._\n"
    assert read_note(forged) is None


def test_a_reason_line_for_another_base_is_ignored():
    body = build_body("i", SECTION, BASE, "Base: mine") + "\n_Compared from fedcba98. Base: someone else wrote this._\n"
    assert read_note(body) == "Base: mine"


def test_the_command_line_reads_the_reason(tmp_path, capsys):
    path = tmp_path / "body.md"
    path.write_text(build_body("i", SECTION, BASE, "Base: the commit before this push."))
    assert pr_body.main(["read-note", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "Base: the commit before this push."
