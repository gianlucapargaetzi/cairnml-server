"""How a repository is reached by a project, for the badges and the dispatch warning of the web app.

It never contains a secret and reads nothing a user could not see: the state comes from bll/repositories.resolve, the same
resolver the check at enqueue and the token of a job use. A repository is recorded for the project when it is asked about
(it is one a run names), owned by the project's owner.
"""

from typing import Optional

from apiserver.bll import repositories, user_access
from apiserver.idp import repo_address
from apiserver.database.model.task.task import Task

NEEDS_DECISION = (repositories.ANONYMOUS, repositories.UNKNOWN, repositories.UNREACHABLE)
""" The states in which the web app asks before a dispatch: nothing is assigned and the company's access does not reach it """


def _row(repository: str, binding, source, resolution, right: Optional[str]) -> dict:
    return {
        "repository": repository,
        "url": f"https://{source.host}/{source.path}",
        "binding": binding.id if binding is not None else None,
        "project": binding.project if binding is not None else None,
        "state": resolution.state,
        "via": resolution.entry.label if resolution.entry is not None else "",
        "message": resolution.message,
        "can_assign": right is not None,
        "ask": resolution.state in NEEDS_DECISION,
    }


def resolve_one(company: str, repository: str, user: str, project: Optional[str], is_admin: bool,
                record: bool = False) -> Optional[dict]:
    """How the project reads the repository. With ``record`` the project's binding is made when missing (the repository
    is one a run names); without it, a repository the project has no binding for is answered from the source alone, and
    nothing is assigned to it."""
    if record:
        binding = repositories.discover(company, repository, user, project)
        source = repositories.source_of(binding) if binding is not None else None
    else:
        binding = repositories.lookup(company, repository, user, project)
        source = repositories.source_of(binding) if binding is not None else repositories.ensure_source(company, repository)
    if source is None:
        return None
    return _row(repository, binding, source, repositories.resolve(source, binding),
                repositories.assigner(binding, user, is_admin) if binding is not None else None)


def resolve(company: str, repository_list: list, user: str, project: Optional[str], is_admin: bool = False) -> list:
    rows = (resolve_one(company, repository, user, project, is_admin) for repository in repository_list if repository)
    return [row for row in rows if row is not None]


def requirement(company: str, user: str, task_ids: list, is_admin: bool) -> list:
    """What each task's repository needs before it is dispatched: one row per distinct repository and project of the
    tasks the user may see, with the ids of those tasks. A row with ``ask`` true is one the web app warns about. A row
    says whether the asking user may assign its credential, and the credentials they may choose from."""
    mine = user_access.my_root_ids(company, user)
    rows: dict = {}
    for task in Task.objects(company=company, id__in=task_ids).only("script.repository", "user", "project"):
        repository = task.script.repository if task.script else None
        root = user_access.root_project(company, task.project) if task.project else None
        if not repository or not (is_admin or task.user == user or root in mine):
            continue
        key = (repo_address.key_of(repository), root)
        if key in rows:
            rows[key]["tasks"].append(str(task.id))
            continue
        row = resolve_one(company, repository, task.user, task.project, False, record=True)
        if row is None:
            continue
        binding = repositories.RepositoryBinding.objects(id=row["binding"]).first() if row["binding"] else None
        source = repositories.source_of(binding) if binding is not None else None
        # the right is the asking user's, not the task owner's
        row["can_assign"] = binding is not None and repositories.assigner(binding, user, is_admin) is not None
        row["assignable"] = repositories.assignable(company, binding, source, user, is_admin) if row["can_assign"] else []
        row["anonymous"] = source.anonymous if source is not None else None
        row["tasks"] = [str(task.id)]
        rows[key] = row
    return list(rows.values())
