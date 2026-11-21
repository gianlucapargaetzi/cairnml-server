"""Hand-written commits on the rolling branch of the sync workflow: when there are some, what the comment says, and how
a run knows that it has commented already. All of it is decided here, not in shell."""

import json

import pytest

from apiserver.contract import manual_commits
from apiserver.contract.manual_commits import (
    BOT_EMAIL,
    MARKER,
    Commit,
    build_comment,
    has_comment,
    manual_commits as find_manual,
    parse_commits,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
BOT1 = "a" * 40
BOT2 = "b" * 40
HUMAN1 = "c" * 40
HUMAN2 = "d" * 40


def _line(sha, email, subject="subject"):
    return f"{sha}\t{email}\t{subject}\n"


def _decide(tmp_path, capsys, text, *extra):
    listing = tmp_path / "commits.tsv"
    listing.write_text(text, encoding="utf-8")
    code = manual_commits.main(["decide", "--commits-file", str(listing), *extra])
    return code, capsys.readouterr()


def test_an_empty_branch_has_no_manual_commit(tmp_path, capsys):
    code, out = _decide(tmp_path, capsys, "")
    assert (code, out.out.strip()) == (0, "none")


def test_only_bot_commits_are_not_manual(tmp_path, capsys):
    text = _line(BOT1, BOT_EMAIL, "Regenerate the API client from cairnml-server 0123abcd") + _line(BOT2, BOT_EMAIL)
    code, out = _decide(tmp_path, capsys, text)
    assert (code, out.out.strip()) == (0, "none")


def test_one_manual_commit_is_found(tmp_path, capsys):
    code, out = _decide(tmp_path, capsys, _line(HUMAN1, "me@example.org", "Adapt the caller"))
    assert (code, out.out.strip()) == (0, "manual 1")


def test_a_manual_commit_with_a_bot_commit_is_found_in_either_order(tmp_path, capsys):
    code, out = _decide(tmp_path, capsys, _line(BOT1, BOT_EMAIL) + _line(HUMAN1, "me@example.org"))
    assert (code, out.out.strip()) == (0, "manual 1")
    code, out = _decide(tmp_path, capsys, _line(HUMAN1, "me@example.org") + _line(BOT1, BOT_EMAIL))
    assert (code, out.out.strip()) == (0, "manual 1")


def test_every_manual_commit_is_counted_and_a_bot_commit_is_not(tmp_path, capsys):
    text = _line(HUMAN1, "me@example.org") + _line(BOT1, BOT_EMAIL) + _line(HUMAN2, "you@example.org")
    code, out = _decide(tmp_path, capsys, text)
    assert (code, out.out.strip()) == (0, "manual 2")


def test_the_bot_email_is_compared_without_regard_to_case_and_the_name_does_not_matter():
    commits = parse_commits(_line(BOT1, BOT_EMAIL.upper()) + _line(HUMAN1, "release@example.org"))
    assert [c.sha for c in find_manual(commits)] == [HUMAN1]


def test_a_commit_that_only_looks_like_the_bot_is_manual():
    commits = parse_commits(_line(HUMAN1, "x" + BOT_EMAIL) + _line(HUMAN2, BOT_EMAIL + ".org"))
    assert [c.sha for c in find_manual(commits)] == [HUMAN1, HUMAN2]


def test_another_bot_email_can_be_named(tmp_path, capsys):
    code, out = _decide(tmp_path, capsys, _line(HUMAN1, "ci@example.org"), "--bot-email", "ci@example.org")
    assert (code, out.out.strip()) == (0, "none")


def test_a_subject_with_tabs_and_a_missing_subject_are_read(tmp_path, capsys):
    text = f"{HUMAN1}\tme@example.org\ta\tb\n{HUMAN2}\tme@example.org\n"
    code, out = _decide(tmp_path, capsys, text)
    assert (code, out.out.strip()) == (0, "manual 2")


@pytest.mark.parametrize(
    "text",
    [
        "not a commit line\n",
        f"{HUMAN1[:39]}\tme@example.org\tshort sha\n",
        f"{HUMAN1.upper()}\tme@example.org\tupper case sha\n",
        f"{HUMAN1}\t\tno email\n",
        f"{HUMAN1}\n",
        _line(BOT1, BOT_EMAIL) + "garbage\n",
    ],
)
def test_a_list_that_cannot_be_read_is_refused_never_read_as_no_manual_commits(tmp_path, capsys, text):
    code, out = _decide(tmp_path, capsys, text)
    assert code == 1
    assert out.out.strip() == ""
    assert "error:" in out.err


def test_a_missing_file_is_refused(tmp_path, capsys):
    assert manual_commits.main(["decide", "--commits-file", str(tmp_path / "missing.tsv")]) == 1
    assert "error:" in capsys.readouterr().err


def test_the_comment_starts_with_the_marker_and_names_the_commits_and_the_server_commit():
    text = build_comment(
        [Commit(HUMAN1, "me@example.org", "Adapt the caller"), Commit(HUMAN2, "you@x.org", "More")], SHA
    )
    assert text.splitlines()[0] == MARKER
    assert "2 commits" in text
    assert f"`{HUMAN1[:8]}` Adapt the caller" in text
    assert f"`{HUMAN2[:8]}` More" in text
    assert "cairnml-server@01234567" in text
    assert "does not push" in text and "does not close" in text
    assert "me@example.org" not in text


def test_the_comment_for_one_commit_is_in_the_singular():
    assert "has 1 commit that" in build_comment([Commit(HUMAN1, "me@example.org", "x")], SHA)


def test_a_commit_message_cannot_break_out_of_its_line_or_into_markup():
    text = build_comment([Commit(HUMAN1, "me@example.org", "a `b` <img src=x>\nsecond line " + "z" * 300)], SHA)
    line = next(line for line in text.splitlines() if line.startswith(f"- `{HUMAN1[:8]}`"))
    assert "<" not in line and ">" not in line and line.count("`") == 2
    assert line.endswith("...") and len(line) < 140
    assert "second line" in line  # one line, not two


def test_a_long_list_is_cut_and_says_how_many_are_left_out():
    commits = [Commit(f"{i:040x}", "me@example.org", f"commit {i}") for i in range(25)]
    text = build_comment(commits, SHA)
    assert text.count("\n- `") == 10
    assert "- and 15 more" in text
    assert "25 commits" in text


def test_the_comment_needs_a_commit_and_a_full_sha():
    with pytest.raises(ValueError):
        build_comment([], SHA)
    with pytest.raises(ValueError):
        build_comment([Commit(HUMAN1, "me@example.org", "x")], "abc")


def test_the_comment_is_written_for_the_manual_commits_only(tmp_path):
    listing = tmp_path / "commits.tsv"
    listing.write_text(
        _line(BOT1, BOT_EMAIL, "bot work") + _line(HUMAN1, "me@example.org", "my work"), encoding="utf-8"
    )
    out = tmp_path / "comment.md"
    assert manual_commits.main(["comment-body", "--commits-file", str(listing), "--sha", SHA, "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "my work" in text and "bot work" not in text and "has 1 commit" in text


def test_the_comment_command_refuses_a_list_without_manual_commits(tmp_path, capsys):
    listing = tmp_path / "commits.tsv"
    listing.write_text(_line(BOT1, BOT_EMAIL), encoding="utf-8")
    out = tmp_path / "comment.md"
    assert manual_commits.main(["comment-body", "--commits-file", str(listing), "--sha", SHA, "--out", str(out)]) == 1
    assert not out.exists()
    assert "error:" in capsys.readouterr().err


def test_a_comment_with_the_marker_on_its_own_line_counts():
    assert has_comment([{"body": "hello"}, {"body": build_comment([Commit(HUMAN1, "m@x.org", "s")], SHA)}])


def test_quoting_the_marker_in_a_sentence_or_an_empty_listing_does_not_count():
    assert not has_comment([])
    assert not has_comment([{"body": f"the marker is {MARKER} in a sentence"}, {"body": None}, {}])


def _has(tmp_path, capsys, text):
    listing = tmp_path / "comments.json"
    listing.write_text(text, encoding="utf-8")
    code = manual_commits.main(["has-comment", "--comments-file", str(listing)])
    return code, capsys.readouterr()


def test_the_comment_listing_is_read_page_by_page(tmp_path, capsys):
    first = json.dumps([{"body": "one"}])
    second = json.dumps([{"body": f"{MARKER}\ntext"}])
    code, out = _has(tmp_path, capsys, first + second)
    assert (code, out.out.strip()) == (0, "yes")
    code, out = _has(tmp_path, capsys, first + json.dumps([{"body": "two"}]))
    assert (code, out.out.strip()) == (0, "no")
    code, out = _has(tmp_path, capsys, "[]")
    assert (code, out.out.strip()) == (0, "no")


@pytest.mark.parametrize("text", ["", "not json", '{"message": "Not Found"}'])
def test_a_comment_listing_that_cannot_be_read_is_refused_never_read_as_no_comment(tmp_path, capsys, text):
    code, out = _has(tmp_path, capsys, text)
    assert code == 1
    assert out.out.strip() == ""
    assert "comment listing" in out.err
