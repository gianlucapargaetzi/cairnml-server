"""What a run does to the repositories: it records the ones it uses, and a dispatch asks how they will be read.

A run records each dataset it used as task parameters under the section ``Datasets`` (cairnml/datasets/_shared.py), as
``<alias>/repo`` (a dataset of its own) or ``<alias>/external_repo`` (the registry an external dataset was read from), and the
repository of its code is the task's script. Each is recorded for the run's project, the moment the run says so, not when it
ends: the record is made for the project's owner when there is none, and the repository is linked to the project (the user who
ran it sees a notice, see bll/access.notices). A registry is read once, so that every dataset it lists is there.
"""

import logging
import re
from typing import Optional, Set, Tuple

from apiserver.bll import repositories as repos
from apiserver.bll import repository_registry, user_access
from apiserver.database.model.auth import User
from apiserver.database.model.project import Project
from apiserver.database.model.repository import Repository
from apiserver.database.model.task.task import Task
from apiserver.idp import repo_address

log = logging.getLogger(__name__)

SECTION = "Datasets"
""" The parameter section the SDK records the datasets of a run under (cairnml/datasets/_shared.py) """

DATASET_KEY, REGISTRY_KEY = "repo", "external_repo"

NEEDS_DECISION = (repos.ANONYMOUS, repos.UNKNOWN, repos.UNREACHABLE)
""" The states in which the web app asks before a dispatch: nothing is assigned and the company's access does not reach it """


def address_of(value: str) -> str:
    """The address a recorded repository names: ``owner/name`` is a GitHub repository."""
    return f"https://github.com/{value}" if re.fullmatch(r"[\w.-]+/[\w.-]+", value or "") else value


def recorded(hyperparams) -> Tuple[Set[str], Set[str]]:
    """(the repositories of datasets of its own, the registries) a run recorded as the datasets it used, from its parameters."""
    datasets, registries = set(), set()
    for name, item in ((hyperparams or {}).get(SECTION) or {}).items():
        alias, _, key = name.rpartition("/")
        if alias and item.value:
            if key == DATASET_KEY:
                datasets.add(item.value)
            elif key == REGISTRY_KEY:
                registries.add(item.value)
    return datasets, registries


def publish_run(task) -> None:
    """Records the repositories a run used and links them to its project. Called when the run records its datasets and when it
    finishes; a repository that is already recorded is left as it is. Never raises for one repository."""
    run = Task.objects(company=task.company, id=task.id).only("id", "company", "user", "project", "hyperparams").first()
    if run is None or not run.user or not user_access.is_active(run.user):
        return
    datasets, registries = recorded(run.hyperparams)
    for repo in sorted(datasets):
        repos.record_use_quietly(run.company, address_of(repo), "dataset", run.user, run.project, run.id)
    for repo in sorted(registries):
        repository_registry.ensure_registry(run.company, address_of(repo), run.user, run.project, run.id)


def record_script(company: str, user: str, task) -> None:
    """The repository a saved task names as its code is recorded for the task's project; a failure never refuses the save.
    ``task`` is the task, or its id."""
    try:
        if isinstance(task, str):
            task = Task.objects(company=company, id=task).only("script.repository", "project").first()
        repository = task.script.repository if task is not None and task.script else None
        if repository:
            repos.record_use_quietly(company, repository, "code", user, task.project)
    except Exception:
        log.warning("the repository of task %s could not be recorded", getattr(task, "id", task), exc_info=True)


# --- the badges and the warning before a dispatch ---------------------------------------------------------------------------------

def _row(repo, address: str, project_root: Optional[str], resolution, can_assign: bool) -> dict:
    return {
        "repository": address, "url": f"https://{repo.host}/{repo.path}", "id": repo.id, "project": project_root,
        "state": resolution.state, "via": resolution.entry.label if resolution.entry is not None else "",
        "message": resolution.message, "warning": resolution.warning, "can_assign": can_assign,
        "ask": resolution.state in NEEDS_DECISION,
    }


def resolve_many(company: str, addresses: list, user: str, project: Optional[str], is_admin: bool = False) -> list:
    """How each repository is read for the user in the project, as a badge shows it. A repository with no record is answered
    from the host alone, and nothing is recorded."""
    root = user_access.root_project(company, project) if project else None
    rows = []
    for address in addresses:
        parsed = repo_address.parse(address or "")
        if parsed is None:
            continue
        repo = repos.find(company, address)
        if repo is None:
            continue
        resolution = repos.resolve(repo, root, user)
        rows.append(_row(repo, address, root, resolution, repos.can_see(company, user, is_admin, repo)))
    return rows


def requirement(company: str, user: str, task_ids: list, is_admin: bool) -> list:
    """What each task's repositories need before they are dispatched by the user: one row per distinct repository and project of
    the tasks the user may see, with the ids of those tasks. A row with ``ask`` true is one the web app warns about. A row says
    whether the user may assign a credential (their own) and the credentials they may choose from."""
    mine = user_access.my_root_ids(company, user)
    rows: dict = {}
    for task in Task.objects(company=company, id__in=task_ids).only("script.repository", "user", "project", "hyperparams"):
        root = user_access.root_project(company, task.project) if task.project else None
        if not (is_admin or task.user == user or root in mine):
            continue
        datasets, registries = recorded(task.hyperparams)
        used = []
        if task.script and task.script.repository:
            used.append((task.script.repository, "code"))
        used += [(address_of(repo), "dataset") for repo in sorted(datasets)]
        used += [(address_of(repo), "registry") for repo in sorted(registries)]
        for address, kind in used:
            key = repo_address.key_of(address)
            if key is None:
                continue
            repo = repos.find(company, address) or repos.record_use_quietly(company, address, kind, task.user, task.project)
            if repo is None:
                continue
            row_key = (key, root)
            if row_key in rows:
                rows[row_key]["tasks"].append(str(task.id))
                if kind not in rows[row_key]["kinds"]:
                    rows[row_key]["kinds"].append(kind)
                continue
            resolution = repos.resolve(repo, root, user)
            can_assign = repos.can_see(company, user, is_admin, repo)
            row = _row(repo, address, root, resolution, can_assign)
            row["assignable"] = [{"id": e.id, "name": e.label, "kind": e.kind, "company": e.scope == "company"}
                                 for e in repos.credential_choices(company, user, repo)] if can_assign else []
            row["anonymous"] = repo.anonymous
            row["tasks"] = [str(task.id)]
            row["kinds"] = [kind]
            rows[row_key] = row
    result = list(rows.values())
    names = {p.id: p.name for p in Project.objects(company=company, id__in=[r["project"] for r in result if r["project"]]).only("name")}
    for row in result:
        row["project_name"] = names.get(row["project"]) if row["project"] else None
    return result


# --- the runs of a repository ---------------------------------------------------------------------------------------------------

MAX_SCANNED, MAX_RUNS = 500, 100


def _uses(task, repo: Repository) -> Optional[str]:
    """How the task uses the repository (``code``, ``dataset`` or ``registry``), or None when it does not."""
    datasets, registries = recorded(task.hyperparams)
    if any(_key(item) == repo.key for item in datasets):
        return "dataset"
    if any(_key(item) == repo.key for item in registries):
        return "registry"
    script = task.script.repository if task.script else None
    return "code" if script and _key(script) == repo.key else None


def _key(address: str) -> Optional[str]:
    parsed = repo_address.parse(address_of(address))
    return parsed.key if parsed else None


def runs_of(company: str, user: str, is_admin: bool, repo: Repository) -> list:
    """The runs that used the repository, newest first, in the projects the user sees (all, for the admin): the runs of the
    projects that list it, read from what each recorded (its datasets and its code), so it is the same list the badges come from."""
    roots = {share.project for share in repos.linked_projects(repo)}
    if not is_admin:
        roots &= user_access.my_root_ids(company, user)
    if not roots:
        return []
    project_ids = set(roots)
    for project in Project.objects(company=company, path__in=list(roots)).only("id"):
        project_ids.add(project.id)
    tasks = Task.objects(company=company, project__in=list(project_ids)).only(
        "id", "name", "status", "project", "user", "created", "hyperparams", "script.repository", "last_update"
    ).order_by("-created").limit(MAX_SCANNED)
    found, users, names = [], {}, {}
    for task in tasks:
        role = _uses(task, repo)
        if role is None:
            continue
        if task.user not in users:
            owner = User.objects(id=task.user).only("name").first() if task.user else None
            users[task.user] = (owner.name if owner is not None else None) or task.user or ""
        if task.project not in names:
            project = Project.objects(company=company, id=task.project).only("name").first()
            names[task.project] = project.name if project is not None else task.project
        found.append({"id": task.id, "name": task.name, "status": task.status, "project": task.project,
                      "project_name": names[task.project], "user": task.user, "user_name": users[task.user],
                      "created": task.created.isoformat() if task.created else None, "as": role})
        if len(found) >= MAX_RUNS:
            break
    return found
