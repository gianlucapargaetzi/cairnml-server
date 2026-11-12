"""The condensed history of a repository: the commits that matter to CairnML, with their branches.

What matters is the commit a task ran on. The graph shows the newest commit, every commit a task ran on (with its tasks; a
task that ran with changes that were not committed is marked), the first commit, and the structure that connects them: the
tip of a branch, the commit a branch forked from and the commit that merged it. The stretches in between are "gap" nodes with
the number of commits they hide.

Branches: the default branch, and every other branch that has a task on a commit of its own (a commit the default branch does
not have). Branches and commits without tasks are not drawn.

One limit for the whole graph: the window ends after 30 commits or when 30 tasks have been listed, newest first, whichever
comes first. The first commit is always shown; what lies between the window and the first commit is one gap.

The rows are newest first, in topological order (a commit before its parents). Each row has a lane (a column of the rail) and
what is needed to draw the lines: the lanes that are open above and below it, the lanes that end in it and the lanes of its
parents. See ``assign_lanes``.
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


def assign_lanes(rows: List[dict]) -> int:
    """Gives every row (``id`` and ``parents``, newest first, a row before its parents) its lane and what draws its lines:
    ``lane``, ``lanes_before`` and ``lanes_after`` (which lanes are open above and below the row), ``merge_from`` (the other
    lanes that end in this row) and ``parent_lanes`` (the lane each parent continues in). Returns the number of lanes."""
    expecting: List[Optional[str]] = []
    count = 1
    for row in rows:
        before = [item is not None for item in expecting]
        ending = [i for i, item in enumerate(expecting) if item == row["id"]]
        if ending:
            lane = ending[0]
        else:
            free = [i for i, item in enumerate(expecting) if item is None]
            lane = free[0] if free else len(expecting)
            if lane == len(expecting):
                expecting.append(None)
        for i in ending:
            expecting[i] = None
        parent_lanes = []
        for number, parent in enumerate(row["parents"]):
            if parent in expecting:
                parent_lanes.append(expecting.index(parent))
                continue
            if number == 0 and expecting[lane] is None:
                slot = lane
            else:
                free = [i for i, item in enumerate(expecting) if item is None]
                slot = free[0] if free else len(expecting)
                if slot == len(expecting):
                    expecting.append(None)
            expecting[slot] = parent
            parent_lanes.append(slot)
        row.update({
            "lane": lane,
            "lanes_before": before + [False] * (len(expecting) - len(before)),
            "lanes_after": [item is not None for item in expecting],
            "merge_from": ending[1:],
            "parent_lanes": parent_lanes,
        })
        count = max(count, len(expecting), lane + 1)
    return count


def condense(commits: List[dict], total: int, first: Optional[dict], runs: List[dict], tips: Optional[Dict[str, List[str]]] = None,
             older: Optional[Dict[str, int]] = None, max_commits: int = MAX_COMMITS, max_tasks: int = MAX_TASKS) -> dict:
    """``commits``: the commits of the branches that count, newest first in topological order, each with ``sha`` and
    ``parents``; ``total`` the number of those commits in the whole history, ``first`` the oldest one, ``runs`` the runs of the
    repository with their ``commit`` and ``dirty``, ``tips`` the branch names by the commit they point at, ``older`` the length of the first-parent chain
    below each parent that is not in ``commits`` (to count the commits a line leaves out; without it the gap is the number of
    commits below the window)."""
    tips = tips or {}
    by_commit = _runs_by_commit(runs)
    window: List[dict] = []
    listed = 0
    for commit in commits:
        window.append(commit)
        listed += len(_runs_of_commit(by_commit, commit["sha"]))
        if len(window) >= max_commits or listed >= max_tasks:
            break
    by_sha = {commit["sha"]: commit for commit in window}
    first_sha = first["sha"] if first else None
    wanted = {commit["sha"] for index, commit in enumerate(window)
              if index == 0 or commit["sha"] == first_sha or commit["sha"] in tips or _runs_of_commit(by_commit, commit["sha"])}

    def ancestors(sha: str) -> set:
        """The commit and everything below it that is in the window."""
        found, todo = set(), [sha]
        while todo:
            current = todo.pop()
            if current in by_sha and current not in found:
                found.add(current)
                todo.extend(by_sha[current]["parents"])
        return found

    # A side branch that merges into a commit counts only when something ran on it; otherwise its merge commit is an ordinary
    # commit of the line it merges into, and the side branch is not drawn.
    kept: Dict[str, List[str]] = {}
    for commit in window:
        parents = commit["parents"]
        if len(parents) > 1:
            line = ancestors(parents[0])
            parents = [parents[0]] + [parent for parent in parents[1:] if (ancestors(parent) - line) & wanted]
        kept[commit["sha"]] = parents
    # the commits that the drawn lines lead through: what is below the commits that count, along the kept parents
    live, todo = set(), list(wanted)
    while todo:
        current = todo.pop()
        if current in by_sha and current not in live:
            live.add(current)
            todo.extend(kept[current])
    children: Dict[str, int] = defaultdict(int)
    for commit in window:
        if commit["sha"] in live:
            for parent in kept[commit["sha"]]:
                children[parent] += 1

    def structural(commit: dict) -> bool:
        return (commit["sha"] in wanted or len(kept[commit["sha"]]) != 1 or children[commit["sha"]] > 1)

    shown = {commit["sha"] for commit in window if commit["sha"] in live and structural(commit)}
    remaining = max(total - len(window) - (0 if first_sha in by_sha else 1), 0) if first else 0

    def resolve(parent: str):
        """The shown commit an edge from a shown commit reaches through commits that are not shown, and how many it skips. An edge
        that leaves the window reaches the first commit, and skips what lies between the window and it."""
        skipped = 0
        while True:
            if parent not in by_sha:
                if older is not None and parent in older:
                    below = 0 if parent == first_sha else max(older[parent] - 1, 0)
                    return first_sha, skipped + below
                return first_sha, skipped + remaining
            if parent in shown:
                return parent, skipped
            skipped += 1
            parent = kept[parent][0]

    def run_rows(sha: str) -> List[dict]:
        return [{k: run.get(k) for k in ("id", "name", "status", "project", "created", "dirty")}
                for run in _runs_of_commit(by_commit, sha)]

    rows: List[dict] = []
    for commit in window:
        if commit["sha"] not in shown:
            continue
        parents, gaps = [], []
        for parent in kept[commit["sha"]]:
            target, skipped = resolve(parent)
            if skipped > 0:
                gap_id = f"gap:{commit['sha']}:{len(gaps)}"
                gaps.append({"id": gap_id, "type": "gap", "count": skipped, "parents": [target]})
                parents.append(gap_id)
            else:
                parents.append(target)
        rows.append({
            "id": commit["sha"], "type": "commit", "sha": commit["sha"], "message": commit.get("message", ""),
            "author": commit.get("author", ""), "date": commit.get("date", ""),
            "newest": commit is window[0], "first": commit["sha"] == first_sha,
            "branches": tips.get(commit["sha"], []), "parents": parents, "runs": run_rows(commit["sha"]),
        })
        rows.extend(gaps)
    if first and first_sha not in by_sha:
        rows.append({
            "id": first_sha, "type": "commit", "sha": first_sha, "message": first.get("message", ""),
            "author": first.get("author", ""), "date": first.get("date", ""), "newest": False, "first": True,
            "branches": tips.get(first_sha, []), "parents": [], "runs": run_rows(first_sha),
        })
    lane_count = assign_lanes(rows)
    in_window = set(by_sha) | ({first_sha} if first_sha else set())
    outside = sum(len(items) for sha, items in by_commit.items()
                  if not any(sha == s or (len(sha) >= 7 and s.startswith(sha)) for s in in_window))
    return {"nodes": [{k: v for k, v in row.items() if k != "parents"} for row in rows], "lane_count": lane_count,
            "total_commits": total, "outside_runs": outside,
            "limited": bool(first and first_sha not in by_sha) or len(window) < len(commits)}


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
                    info.get("older"))


def _resolved(runs: List[dict], resolved: Dict[str, str]) -> List[dict]:
    """The runs with the full sha of their commit, when git knows it (a run may have recorded a short sha)."""
    return [{**run, "commit": resolved.get(run.get("commit"), run.get("commit"))} for run in runs]
