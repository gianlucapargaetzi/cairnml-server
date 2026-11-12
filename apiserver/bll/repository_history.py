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
    can. Returns the number of lanes."""
    expecting: List[Optional[str]] = [None]
    count = 1

    def free_lane() -> int:
        free = [i for i, item in enumerate(expecting) if item is None and i > 0]
        if free:
            return free[0]
        expecting.append(None)
        return len(expecting) - 1

    for row in rows:
        ending = [i for i, item in enumerate(expecting) if item == row["id"]]
        if row["id"] in main_ids:
            lane = 0
        elif ending:
            lane = ending[0]
        else:
            lane = free_lane()
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
                expecting[free_lane()] = parent
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
              if index == 0 or commit["sha"] in tips or _runs_of_commit(by_commit, commit["sha"])}

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

    shown = {commit["sha"] for commit in window
             if commit["sha"] in live and (commit["sha"] in wanted or len(kept[commit["sha"]]) > 1 or children[commit["sha"]] > 1)}

    def resolve(parent: str):
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

    def run_rows(sha: str) -> List[dict]:
        return [{k: run.get(k) for k in ("id", "name", "status", "project", "created", "dirty")}
                for run in _runs_of_commit(by_commit, sha)]

    rows: List[dict] = []
    for commit in window:
        if commit["sha"] not in shown:
            continue
        parents, gaps = [], []
        for number, parent in enumerate(kept[commit["sha"]]):
            target, skipped = resolve(parent)
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
            "branches": tips.get(commit["sha"], []), "parents": parents, "runs": run_rows(commit["sha"]),
        })
        rows.extend(gaps)

    by_id = {row["id"]: row for row in rows}
    main_ids, current = set(), default_tip if default_tip in by_id else (rows[0]["id"] if rows else None)
    while current in by_id and current not in main_ids:
        main_ids.add(current)
        current = by_id[current]["parents"][0] if by_id[current]["parents"] else None
    lane_count = assign_lanes(rows, main_ids)
    index = {row["id"]: number for number, row in enumerate(rows)}
    nodes = []
    for row in rows:
        node = {key: row[key] for key in NODE_FIELDS if key in row}
        node["parent_rows"] = [index.get(parent, -1) for parent in row["parents"]]
        nodes.append(node)
    outside = sum(len(items) for sha, items in by_commit.items()
                  if not any(sha == s or (len(sha) >= 7 and s.startswith(sha)) for s in by_sha))
    return {"nodes": nodes, "lane_count": lane_count, "total_commits": total, "outside_runs": outside,
            "limited": total > len(window)}


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
