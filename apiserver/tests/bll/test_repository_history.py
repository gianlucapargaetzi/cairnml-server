"""The condensed history of a repository."""

from apiserver.bll.repository_history import condense


def commit(number: int) -> dict:
    return {"sha": f"{number:040x}", "message": f"commit {number}", "author": "a", "date": f"2026-01-{number:02d}T00:00:00+00:00",
            "parents": []}


def history(total: int):
    """``total`` commits numbered 1 (the first) to ``total`` (the newest), newest first, and the first commit."""
    commits = [commit(n) for n in range(total, 0, -1)]
    return commits, commits[-1]


def run(number: int, name="run", dirty=False, run_id=None):
    return {"id": run_id or f"{name}-{number}", "name": name, "status": "completed", "project": "p", "created": None,
            "commit": commit(number)["sha"], "dirty": dirty}


def shape(result):
    """The nodes as the owner writes them: 20, '...', 11, 10, '...', 2, 1 (newest first); a gap as its count."""
    out = []
    for node in result["nodes"]:
        out.append(f"...{node['count']}" if node["type"] == "gap" else int(node["sha"], 16))
    return out


def test_the_example_of_twenty_commits_with_runs_on_2_10_and_11():
    commits, first = history(20)
    result = condense(commits, 20, first, [run(2), run(10), run(11)])
    assert shape(result) == [20, "...8", 11, 10, "...7", 2, 1]
    # 12..19 hide 8 commits, 3..9 hide 7; 11 and 10 follow each other, 2 and 1 follow each other


def test_the_first_and_the_newest_commit_are_always_shown_even_without_runs():
    commits, first = history(5)
    result = condense(commits, 5, first, [])
    assert shape(result) == [5, "...3", 1]
    assert result["nodes"][0]["newest"] is True
    assert result["nodes"][-1]["first"] is True


def test_a_short_history_with_runs_everywhere_has_no_gap():
    commits, first = history(3)
    result = condense(commits, 3, first, [run(1), run(2), run(3)])
    assert shape(result) == [3, 2, 1]


def test_a_commit_lists_its_runs_and_marks_the_ones_with_uncommitted_changes():
    commits, first = history(3)
    result = condense(commits, 3, first, [run(2, "a"), run(2, "b", dirty=True)])
    node = next(n for n in result["nodes"] if n["type"] == "commit" and int(n["sha"], 16) == 2)
    assert [(r["name"], r["dirty"]) for r in node["runs"]] == [("a", False), ("b", True)]


def test_a_run_that_recorded_a_short_commit_is_found():
    import hashlib

    commits = [{"sha": hashlib.sha1(str(n).encode()).hexdigest(), "message": str(n), "author": "a", "date": "", "parents": []}
               for n in (4, 3, 2, 1)]
    short = {"id": "x", "name": "x", "status": "completed", "project": "p", "created": None, "commit": commits[1]["sha"][:10], "dirty": False}
    result = condense(commits, 4, commits[-1], [short])
    assert [n["message"] if n["type"] == "commit" else f"...{n['count']}" for n in result["nodes"]] == ["4", "3", "...1", "1"]
    assert [len(n["runs"]) for n in result["nodes"] if n["type"] == "commit"] == [0, 1, 0]


def test_only_the_latest_thirty_commits_are_shown_and_the_rest_is_one_gap_to_the_first():
    commits, first = history(100)
    result = condense(commits[:30], 100, first, [run(80)])
    assert shape(result) == [100, "...19", 80, "...78", 1]  # the window is 100..71; 79..2 are 78 commits
    assert result["limited"] is True
    assert shape(condense(commits[:30], 100, first, [])) == [100, "...98", 1]


def test_the_window_ends_when_thirty_tasks_are_listed():
    commits, first = history(100)
    runs = [run(90, f"t{i}") for i in range(30)] + [run(50, "old")]
    result = condense(commits, 100, first, runs)
    assert 90 in shape(result)
    assert 50 not in shape(result)
    assert result["outside_runs"] == 1


def test_runs_on_unknown_commits_are_counted_not_shown():
    commits, first = history(4)
    stray = {"id": "s", "name": "s", "status": "failed", "project": "p", "created": None, "commit": "f" * 40, "dirty": False}
    assert condense(commits, 4, first, [stray])["outside_runs"] == 1


def test_a_repository_with_one_commit():
    only = commit(1)
    result = condense([only], 1, only, [run(1)])
    assert shape(result) == [1]
    assert result["nodes"][0]["newest"] and result["nodes"][0]["first"]
