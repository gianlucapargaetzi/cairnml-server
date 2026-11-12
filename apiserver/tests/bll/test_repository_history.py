"""The condensed history of a repository, with its branches."""

import hashlib

from apiserver.bll.repository_history import assign_lanes, condense


def sha(number: int) -> str:
    return f"{number:040x}"


def commit(number: int, parents=None) -> dict:
    """Commit ``number``; by default its parent is ``number - 1`` (a straight line)."""
    if parents is None:
        parents = [sha(number - 1)] if number > 1 else []
    return {"sha": sha(number), "message": f"commit {number}", "author": "a", "date": f"2026-01-{number % 28 + 1:02d}T00:00:00+00:00",
            "parents": parents}


def history(total: int):
    """``total`` commits numbered 1 (the first) to ``total`` (the newest), newest first, and the first commit."""
    commits = [commit(n) for n in range(total, 0, -1)]
    return commits, commits[-1]


def run(number: int, name="run", dirty=False):
    return {"id": f"{name}-{number}", "name": name, "status": "completed", "project": "p", "created": None,
            "commit": sha(number), "dirty": dirty}


def shape(result):
    """The nodes as the owner writes them: 20, '...8', 11, 10, ... (newest first); a gap as its count."""
    return [f"...{node['count']}" if node["type"] == "gap" else int(node["sha"], 16) for node in result["nodes"]]


def test_the_example_of_twenty_commits_with_runs_on_2_10_and_11():
    commits, first = history(20)
    result = condense(commits, 20, first, [run(2), run(10), run(11)], default_tip=sha(20))
    assert shape(result) == [20, "...8", 11, 10, "...7", 2, "...1"]
    assert result["lane_count"] == 1
    assert {node["lane"] for node in result["nodes"]} == {0}
    # consecutive commits are connected directly: 11 -> 10, and 2 -> the commit hidden below it
    assert result["nodes"][2]["parent_rows"] == [3]
    assert result["nodes"][3]["parent_rows"] == [4]


def test_the_newest_commit_is_always_shown_the_first_only_when_it_has_a_task():
    commits, first = history(5)
    result = condense(commits, 5, first, [], default_tip=sha(5))
    assert shape(result) == [5, "...4"]
    assert result["nodes"][0]["newest"] is True
    with_task = condense(commits, 5, first, [run(1)], default_tip=sha(5))
    assert shape(with_task) == [5, "...3", 1]
    assert with_task["nodes"][-1]["first"] is True


def test_a_short_history_with_runs_everywhere_has_no_gap():
    commits, first = history(3)
    assert shape(condense(commits, 3, first, [run(1), run(2), run(3)], default_tip=sha(3))) == [3, 2, 1]


def test_a_commit_lists_its_runs_and_marks_the_ones_with_uncommitted_changes():
    commits, first = history(3)
    result = condense(commits, 3, first, [run(2, "a"), run(2, "b", dirty=True)], default_tip=sha(3))
    node = next(n for n in result["nodes"] if n["type"] == "commit" and int(n["sha"], 16) == 2)
    assert [(r["name"], r["dirty"]) for r in node["runs"]] == [("a", False), ("b", True)]


def test_a_run_that_recorded_a_short_commit_is_found():
    commits = [{"sha": hashlib.sha1(str(n).encode()).hexdigest(), "message": str(n), "author": "a", "date": "", "parents": []}
               for n in (4, 3, 2, 1)]
    for older, newer in zip(commits[1:], commits[:-1]):
        newer["parents"] = [older["sha"]]
    short = {"id": "x", "name": "x", "status": "completed", "project": "p", "created": None, "commit": commits[1]["sha"][:10], "dirty": False}
    result = condense(commits, 4, commits[-1], [short])
    assert [n["message"] if n["type"] == "commit" else f"...{n['count']}" for n in result["nodes"]] == ["4", "3", "...2"]
    assert [len(n["runs"]) for n in result["nodes"] if n["type"] == "commit"] == [0, 1]


def test_a_line_that_reaches_the_end_of_the_window_runs_off_the_bottom():
    commits, first = history(100)
    result = condense(commits[:30], 100, first, [run(80)], default_tip=sha(100))
    assert shape(result) == [100, "...19", 80, "...9"]  # the window is 100..71
    assert result["limited"] is True
    assert result["nodes"][-1]["parent_rows"] == [-1]  # the last gap has no commit below it in the list
    assert shape(condense(commits[:30], 100, first, [], default_tip=sha(100))) == [100, "...29"]


def test_the_window_ends_when_thirty_tasks_are_listed():
    commits, first = history(100)
    runs = [run(90, f"t{i}") for i in range(30)] + [run(50, "old")]
    result = condense(commits, 100, first, runs, default_tip=sha(100))
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
    assert result["limited"] is False


# --- branches ----------------------------------------------------------------------------------------------------------

def two_lines():
    """main: 1-2-3-4-5 and feature, forked from 2: 2-6-7 (newer than main). Newest first in topological order."""
    commits = [commit(7, [sha(6)]), commit(6, [sha(2)]), commit(5), commit(4), commit(3), commit(2), commit(1)]
    return commits, commits[-1]


def test_the_default_branch_is_always_the_left_lane_even_when_a_branch_is_newer():
    commits, first = two_lines()
    result = condense(commits, 7, first, [run(6, "on the branch")], tips={sha(7): ["feature"], sha(5): ["main"]},
                      default_tip=sha(5))
    assert shape(result) == [7, 6, 5, "...2", 2, "...1"]
    nodes = result["nodes"]
    assert [n["lane"] for n in nodes] == [1, 1, 0, 0, 0, 0]
    assert result["lane_count"] == 2
    assert nodes[0]["branches"] == ["feature"] and nodes[2]["branches"] == ["main"]
    # the branch line runs from 7 to 6 and from 6 to the fork point 2; the main line from 5 down through its gap to 2
    assert nodes[0]["parent_rows"] == [1]
    assert nodes[1]["parent_rows"] == [4]
    assert nodes[2]["parent_rows"] == [3] and nodes[3]["parent_rows"] == [4]


def test_a_merge_commit_is_shown_with_both_of_its_lines():
    # 1-2-3 on main, 2-4 on a branch, merged by 5 (parents 3 and 4)
    commits = [commit(5, [sha(3), sha(4)]), commit(4, [sha(2)]), commit(3), commit(2), commit(1)]
    result = condense(commits, 5, commits[-1], [run(4)], tips={sha(5): ["main"]}, default_tip=sha(5))
    assert shape(result) == [5, "...1", 4, 2, "...1"]  # 3 has no task: it is the gap on the main line
    assert [n["lane"] for n in result["nodes"]] == [0, 0, 1, 0, 0]
    assert result["nodes"][0]["parent_rows"] == [1, 2]
    assert result["lane_count"] == 2


def test_assign_lanes_keeps_lane_zero_for_the_main_line_and_moves_a_line_back_left():
    rows = [{"id": "d", "parents": ["b", "c"]}, {"id": "c", "parents": ["a"]}, {"id": "b", "parents": ["a"]},
            {"id": "a", "parents": []}]
    assert assign_lanes(rows, {"d", "b", "a"}) == 2
    assert [row["lane"] for row in rows] == [0, 1, 0, 0]


def test_no_other_line_takes_lane_zero():
    rows = [{"id": "x", "parents": ["y"]}, {"id": "m", "parents": []}, {"id": "y", "parents": []}]
    assign_lanes(rows, {"m"})
    assert [row["lane"] for row in rows] == [1, 0, 1]


def test_a_side_branch_without_tasks_is_not_drawn_and_its_merge_is_an_ordinary_commit():
    commits = [commit(5, [sha(3), sha(4)]), commit(4, [sha(2)]), commit(3), commit(2), commit(1)]
    result = condense(commits, 5, commits[-1], [run(3)], tips={sha(5): ["main"]}, default_tip=sha(5))
    assert shape(result) == [5, 3, "...2"]
    assert result["lane_count"] == 1


def test_a_merged_side_branch_with_a_task_keeps_its_lane():
    commits = [commit(5, [sha(3), sha(4)]), commit(4, [sha(2)]), commit(3), commit(2), commit(1)]
    result = condense(commits, 5, commits[-1], [run(4)], tips={sha(5): ["main"]}, default_tip=sha(5))
    assert result["lane_count"] == 2
    assert 4 in shape(result)
