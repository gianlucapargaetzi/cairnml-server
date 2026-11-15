"""The condensed history of a repository: the commits that matter to CairnML, with their branches.

What matters is the commit a task ran on. The graph shows the newest commit, every commit a task ran on (with its tasks; a
task that ran with changes that were not committed is marked), the first commit, and the structure that connects them: the
tip of a branch, the commit a branch forked from and the commit that merged it. The stretches in between are "gap" nodes with
the number of commits they hide.

Branches: the default branch, and every other branch that has a task on a commit of its own (a commit the default branch does
not have). Branches and commits without tasks are not drawn.

One limit for the whole graph: the window ends after 30 commits or when 30 tasks have been listed, newest first, whichever
comes first. A line that reaches the end of the window simply runs off the bottom of the list.

The rows are newest first, in topological order (a commit before its parents). Each row has a lane (a column of the rail) and
and the rows of its parents (``parent_rows``, -1 for a line that leaves the window), which is all the page needs to draw
the lines as curves from commit to commit. Lane 0 is the default branch. See ``assign_lanes``.
"""

from collections import defaultdict
from typing import Dict, List, Optional

from apiserver.apierrors import errors
from apiserver.bll import repository_registry as registry_bll
from apiserver.bll import repository_runs
from apiserver.idp.github import GitHubError

MAX_COMMITS = 30
MAX_TASKS = 30


def _runs_by_commit(runs: List[dict]) -> Dict[str, List[dict]]:
    by_commit: Dict[str, List[dict]] = defaultdict(list)
    for run in runs:
        if run.get("commit"):
            by_commit[run["commit"]].append(run)
    return by_commit


def _runs_of_commit(by_commit: Dict[str, List[dict]], sha: str) -> List[dict]:
    """The runs recorded for a commit; a run may have recorded a shortened sha."""
    found = list(by_commit.get(sha, []))
    for recorded, runs in by_commit.items():
        if recorded != sha and len(recorded) >= 7 and sha.startswith(recorded):
            found.extend(runs)
    return found


def assign_lanes(rows: List[dict], main_ids=frozenset()) -> int:
    """Gives every row (``id`` and ``parents``, newest first, a row before its parents) its ``lane``. Lane 0 belongs to the
    default branch: the rows in ``main_ids`` are always in it, and no other row ever is. A line keeps its lane for as long as it
    can, and every other branch has a lane of its own. Returns the number of lanes."""
    expecting: List[Optional[str]] = [None]
    count = 1

    def new_lane() -> int:
        """A lane of its own: a lane is never reused for another branch, so every branch in the list has its own column and colour."""
        expecting.append(None)
        return len(expecting) - 1

    for row in rows:
        ending = [i for i, item in enumerate(expecting) if item == row["id"]]
        if row["id"] in main_ids:
            lane = 0
        elif ending:
            lane = ending[0]
        else:
            lane = new_lane()
        for i in ending:
            expecting[i] = None
        for number, parent in enumerate(row["parents"]):
            if parent in expecting:
                other = expecting.index(parent)
                if number == 0 and other > lane and expecting[lane] is None and (lane > 0 or parent in main_ids):
                    # the line this commit continues is already open in a lane to the right: it moves left into this lane
                    expecting[other], expecting[lane] = None, parent
                continue
            if number == 0 and expecting[lane] is None:
                expecting[lane] = parent
            else:
                expecting[new_lane()] = parent
        row["lane"] = lane
        count = max(count, len(expecting), lane + 1)
    return count


NODE_FIELDS = ("type", "sha", "message", "author", "date", "newest", "first", "branches", "runs", "count", "lane")


def condense(commits: List[dict], total: int, first: Optional[dict], runs: List[dict], tips: Optional[Dict[str, List[str]]] = None,
             max_commits: int = MAX_COMMITS, max_tasks: int = MAX_TASKS, default_tip: Optional[str] = None) -> dict:
    """``commits``: the commits of the branches that count, newest first in topological order, each with ``sha`` and
    ``parents``; ``total`` the number of those commits in the whole history, ``first`` the oldest one (it is only marked, never
    added), ``runs`` the runs of the repository with their ``commit`` and ``dirty``, ``tips`` the branch names by the commit they
    point at and ``default_tip`` the commit the default branch points at (it, and the line below it, take lane 0).

    A line that reaches the end of the window is not closed: its row says so with ``-1`` in ``parent_rows`` and the line runs
    off the bottom of the list."""
    tips = tips or {}
    by_commit = _runs_by_commit(runs)
    window = _window(commits, by_commit, max_commits, max_tasks)
    by_sha = {commit["sha"]: commit for commit in window}
    wanted = _wanted(window, by_commit, tips)
    kept = _kept_parents(window, wanted, by_sha)
    live = _live(wanted, by_sha, kept)
    children = _children(window, live, kept)
    shown = _shown(window, live, wanted, kept, children)
    rows = _rows(window, shown, kept, by_sha, by_commit, tips, first["sha"] if first else None)
    lane_count = assign_lanes(rows, _main_line(rows, default_tip))
    return {"nodes": _nodes(rows), "lane_count": lane_count, "total_commits": total,
            "outside_runs": _outside_runs(by_commit, by_sha), "limited": total > len(window)}


def _window(commits: List[dict], by_commit: dict, max_commits: int, max_tasks: int) -> List[dict]:
    """The newest commits, until ``max_commits`` are taken or the runs on them reach ``max_tasks``."""
    window: List[dict] = []
    listed = 0
    for commit in commits:
        window.append(commit)
        listed += len(_runs_of_commit(by_commit, commit["sha"]))
        if len(window) >= max_commits or listed >= max_tasks:
            break
    return window


def _wanted(window: List[dict], by_commit: dict, tips: Dict[str, List[str]]) -> set:
    """The commits that count: the newest, the ones a branch points at, and the ones a run was on."""
    return {commit["sha"] for index, commit in enumerate(window)
            if index == 0 or commit["sha"] in tips or _runs_of_commit(by_commit, commit["sha"])}


def _ancestors(sha: str, by_sha: Dict[str, dict]) -> set:
    """The commit and everything below it that is in the window."""
    found, todo = set(), [sha]
    while todo:
        current = todo.pop()
        if current in by_sha and current not in found:
            found.add(current)
            todo.extend(by_sha[current]["parents"])
    return found


def _kept_parents(window: List[dict], wanted: set, by_sha: Dict[str, dict]) -> Dict[str, List[str]]:
    """The parents each commit is drawn with. A side branch that merges into a commit counts only when something ran on it;
    otherwise its merge commit is an ordinary commit of the line it merges into, and the side branch is not drawn."""
    kept: Dict[str, List[str]] = {}
    for commit in window:
        parents = commit["parents"]
        if len(parents) > 1:
            line = _ancestors(parents[0], by_sha)
            parents = [parents[0]] + [parent for parent in parents[1:] if (_ancestors(parent, by_sha) - line) & wanted]
        kept[commit["sha"]] = parents
    return kept


def _live(wanted: set, by_sha: Dict[str, dict], kept: Dict[str, List[str]]) -> set:
    """The commits that the drawn lines lead through: what is below the commits that count, along the kept parents."""
    live, todo = set(), list(wanted)
    while todo:
        current = todo.pop()
        if current in by_sha and current not in live:
            live.add(current)
            todo.extend(kept[current])
    return live


def _children(window: List[dict], live: set, kept: Dict[str, List[str]]) -> Dict[str, int]:
    children: Dict[str, int] = defaultdict(int)
    for commit in window:
        if commit["sha"] in live:
            for parent in kept[commit["sha"]]:
                children[parent] += 1
    return children


def _shown(window: List[dict], live: set, wanted: set, kept: Dict[str, List[str]], children: Dict[str, int]) -> set:
    """The commits drawn as rows: the ones that count, and the ones where lines meet or split."""
    return {commit["sha"] for commit in window
            if commit["sha"] in live and (commit["sha"] in wanted or len(kept[commit["sha"]]) > 1 or children[commit["sha"]] > 1)}


def _resolve_edge(parent: str, by_sha: Dict[str, dict], shown: set, kept: Dict[str, List[str]]):
    """Where an edge from a shown commit leads through commits that are not shown: (the shown commit, how many it skips). The
    shown commit is None when the line ends in the root, and ``""`` when it leaves the window."""
    skipped = 0
    while True:
        if parent not in by_sha:
            return "", skipped
        if parent in shown:
            return parent, skipped
        skipped += 1
        if not kept[parent]:
            return None, skipped
        parent = kept[parent][0]


def _run_rows(by_commit: dict, sha: str) -> List[dict]:
    return [{k: run.get(k) for k in ("id", "name", "status", "project", "created", "dirty")}
            for run in _runs_of_commit(by_commit, sha)]


def _rows(window: List[dict], shown: set, kept: Dict[str, List[str]], by_sha: Dict[str, dict], by_commit: dict,
          tips: Dict[str, List[str]], first_sha: Optional[str]) -> List[dict]:
    """A row for each shown commit, followed by the gap rows its lines skip over."""
    rows: List[dict] = []
    for commit in window:
        if commit["sha"] not in shown:
            continue
        parents, gaps = [], []
        for number, parent in enumerate(kept[commit["sha"]]):
            target, skipped = _resolve_edge(parent, by_sha, shown, kept)
            leave = f"off:{commit['sha']}:{number}"
            if target == "":
                target = leave
            if skipped > 0:
                gap_id = f"gap:{commit['sha']}:{number}"
                gaps.append({"id": gap_id, "type": "gap", "count": skipped, "parents": [target] if target else []})
                parents.append(gap_id)
            else:
                parents.append(target)
        rows.append({
            "id": commit["sha"], "type": "commit", "sha": commit["sha"], "message": commit.get("message", ""),
            "author": commit.get("author", ""), "date": commit.get("date", ""),
            "newest": commit is window[0], "first": commit["sha"] == first_sha,
            "branches": tips.get(commit["sha"], []), "parents": parents, "runs": _run_rows(by_commit, commit["sha"]),
        })
        rows.extend(gaps)
    return rows


def _main_line(rows: List[dict], default_tip: Optional[str]) -> set:
    """The rows of the default branch, from its tip (or the newest row) down the first parents."""
    by_id = {row["id"]: row for row in rows}
    main_ids, current = set(), default_tip if default_tip in by_id else (rows[0]["id"] if rows else None)
    while current in by_id and current not in main_ids:
        main_ids.add(current)
        current = by_id[current]["parents"][0] if by_id[current]["parents"] else None
    return main_ids


def _nodes(rows: List[dict]) -> List[dict]:
    index = {row["id"]: number for number, row in enumerate(rows)}
    nodes = []
    for row in rows:
        node = {key: row[key] for key in NODE_FIELDS if key in row}
        node["parent_rows"] = [index.get(parent, -1) for parent in row["parents"]]
        nodes.append(node)
    return nodes


def _outside_runs(by_commit: dict, by_sha: Dict[str, dict]) -> int:
    """The runs on commits that are not in the window (a short sha names a commit by its prefix)."""
    return sum(len(items) for sha, items in by_commit.items()
               if not any(sha == s or (len(sha) >= 7 and s.startswith(sha)) for s in by_sha))


def history(company: str, user: str, is_admin: bool, repo) -> dict:
    """The condensed history of a repository the user sees, read over git with the credential that reads it."""
    runs = repository_runs.runs_of(company, user, is_admin, repo)
    reader = registry_bll.reader_for(repo, user)
    try:
        info = reader.graph_history(repo.path, sorted({run["commit"] for run in runs if run.get("commit")}), MAX_COMMITS)
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"the history of {repo.path} could not be read ({exc})") from None
    finally:
        getattr(reader, "close", lambda: None)()
    return condense(info["commits"], info["total"], info["first"], _resolved(runs, info["resolved"]), info["tips"],
                    default_tip=info.get("default_tip"))


def _resolved(runs: List[dict], resolved: Dict[str, str]) -> List[dict]:
    """The runs with the full sha of their commit, when git knows it (a run may have recorded a short sha)."""
    return [{**run, "commit": resolved.get(run.get("commit"), run.get("commit"))} for run in runs]
