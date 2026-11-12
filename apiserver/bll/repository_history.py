"""The condensed history of a repository: the commits that matter to CairnML.

What matters is the commit a task ran on. The history shows the newest commit, every commit a task ran on (with its tasks;
a task that ran with changes that were not committed is marked), and the first commit; the stretches in between are one
"gap" node with the number of commits it hides. Commits that follow each other are connected without a gap.

Only the latest part is shown: the window ends after 30 commits or when 30 tasks have been listed, whichever comes first.
The first commit is always shown, and what lies between the window and the first commit is one gap.

Newest first, as the commit graph of the repository panel is.
"""

from collections import defaultdict
from typing import Dict, List, Optional

from apiserver.bll import repository_runs
from apiserver.bll import repository_registry as registry_bll
from apiserver.bll import repositories as repos
from apiserver.apierrors import errors
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


def condense(commits: List[dict], total: int, first: Optional[dict], runs: List[dict],
             max_commits: int = MAX_COMMITS, max_tasks: int = MAX_TASKS) -> dict:
    """``commits`` newest first (the window to look at, at least the newest ``max_commits``), ``total`` the number of commits of
    the whole history, ``first`` the oldest commit, ``runs`` the runs of the repository with their ``commit`` and ``dirty``."""
    by_commit = _runs_by_commit(runs)
    window: List[dict] = []
    listed = 0
    for commit in commits:
        window.append(commit)
        listed += len(_runs_of_commit(by_commit, commit["sha"]))
        if len(window) >= max_commits or listed >= max_tasks:
            break

    # (position in the whole history, commit, its runs), oldest position last
    shown = []
    for index, commit in enumerate(window):
        commit_runs = _runs_of_commit(by_commit, commit["sha"])
        if index == 0 or commit_runs or (first and commit["sha"] == first["sha"]):
            shown.append((index, commit, commit_runs))
    if first and (not shown or shown[-1][1]["sha"] != first["sha"]):
        shown.append((max(total - 1, len(window)), first, _runs_of_commit(by_commit, first["sha"])))

    placed = {commit["sha"] for _, commit, _ in shown}
    in_window = {commit["sha"] for commit in window} | ({first["sha"]} if first else set())
    outside = sum(len(items) for sha, items in by_commit.items()
                  if not any(sha == s or (len(sha) >= 7 and s.startswith(sha)) for s in in_window))

    nodes: List[dict] = []
    previous: Optional[int] = None
    for position, commit, commit_runs in shown:
        if previous is not None and position - previous - 1 > 0:
            nodes.append({"type": "gap", "count": position - previous - 1})
        nodes.append({
            "type": "commit", "sha": commit["sha"], "message": commit.get("message", ""),
            "author": commit.get("author", ""), "date": commit.get("date", ""),
            "newest": position == 0, "first": bool(first and commit["sha"] == first["sha"]),
            "runs": [{k: run.get(k) for k in ("id", "name", "status", "project", "created", "dirty")} for run in commit_runs],
        })
        previous = position
    return {"nodes": nodes, "total_commits": total, "outside_runs": outside,
            "limited": bool(first and first["sha"] not in {c["sha"] for c in window}) or len(window) < len(commits)}


def history(company: str, user: str, is_admin: bool, repo) -> dict:
    """The condensed history of a repository the user sees, read over git with the credential that reads it."""
    runs = repository_runs.runs_of(company, user, is_admin, repo)
    reader = registry_bll.reader_for(repo, user)
    try:
        info = reader.commit_history(repo.path, MAX_COMMITS)
    except GitHubError as exc:
        raise errors.bad_request.ValidationError(f"the history of {repo.path} could not be read ({exc})") from None
    finally:
        getattr(reader, "close", lambda: None)()
    return condense(info["commits"], info["total"], info["first"], runs)
