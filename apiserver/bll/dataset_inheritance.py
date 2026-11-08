"""Implicit sharing after a run: the datasets a finished run used are shared with its project, as inherited shares.

A run records each dataset it used as task parameters under the section ``Datasets`` (cairnml/datasets/_shared.py,
``SECTION``), as ``<alias>/repo`` (a local dataset) or ``<alias>/external_repo`` (an external one). The repository of
each is matched, case-insensitively, to a kept dataset (kind repo) of the company.

For each dataset, when the root project of the run has no share of it yet, a dataset share at Read is made with
inherited=True and run_id=the run's id. The project's share is granted by the root project's owner (R20), as the
allocation writers grant it. The dataset's credential is shared the same way only when the run's user owns it: a
credential of another user is shared with a project only by its owner (decision 3). The run's user is the task's
owner (Task.user), which stands in for the enqueuer, as queue entries do not record one.

Nothing is shared for a deactivated run user (R25). A dataset the run's user cannot use (bll/access: level_of with the
user's project roots, and the use action) is skipped, and so is its credential: a run never shares what its user could
not read.
"""

from typing import List, Optional, Set

from apiserver.bll import access, user_access
from apiserver.database.model.auth import User
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Task
from apiserver.database.model.user_access import UserAccess

SECTION = "Datasets"
""" The parameter section the SDK records the datasets of a run under (cairnml/datasets/_shared.py) """

REPO_KEYS = ("repo", "external_repo")
""" The parameters that name the repository of a recorded dataset """


def _recorded_repos(company: str, task_id: str) -> Set[str]:
    run = Task.objects(company=company, id=task_id).only("hyperparams").first()
    section = (run.hyperparams or {}).get(SECTION) if run else None
    repos = set()
    for name, item in (section or {}).items():
        alias, _, key = name.rpartition("/")
        if alias and key in REPO_KEYS and item.value:
            repos.add(item.value)
    return repos


def _datasets_of(company: str, task_id: str) -> List[Dataset]:
    found = {}
    for repo in _recorded_repos(company, task_id):
        for dataset in Dataset.objects(company=company, kind="repo", ref__iexact=repo).only("id", "credential"):
            found[dataset.id] = dataset
    return list(found.values())


def _run_user_can_use(company: str, user: str, dataset_id: str) -> bool:
    """True when the run's user holds a level on the dataset that allows its use, through the share records."""
    found = User.objects(id=user, company=company).only("role").first()
    is_admin = found is not None and found.role in access.ADMIN_ROLES
    roots = {project["id"] for project in user_access.my_projects(company, user)}
    level = access.level_of(company, user, is_admin, "dataset", dataset_id, roots)
    return access.allows(level, "use", "dataset")


def _root_owner(company: str, root: str) -> Optional[str]:
    project = Project.objects(company=company, id=root).only("user").first()
    return project.user if project else None


def _share_credential(company: str, run: Task, root: str, credential_id: str) -> Optional[Share]:
    entry = UserAccess.objects(company=company, id=credential_id).only("user").first()
    if entry is None or entry.user != run.user:
        return None
    if access.find_share(company, "credential", entry.id, "projects", root) is not None:
        return None
    return access.record_grant(company, entry.user, "credential", entry.id, "projects", root, level="read",
                               actor=run.user, inherited=True, run_id=run.id)


def _credential_needed_by_others(company: str, project: str, credential_id: str, dataset_id: str) -> bool:
    """Whether another dataset shared with the project still uses the credential."""
    others = [share.resource_id for share in Share.objects(
        company=company, resource_type="dataset", scope="projects", project=project) if share.resource_id != dataset_id]
    return bool(others) and Dataset.objects(company=company, id__in=others, credential=credential_id).count() > 0


def release_credential_of_revoked(company: str, share: Share) -> None:
    """After the share of a dataset with a project is revoked: the dataset's credential is removed from the project too,
    when the credential's share was inherited and no other dataset shared with the project still needs it. A credential
    shared by hand is kept, as its owner chose it."""
    dataset = Dataset.objects(company=company, id=share.resource_id).only("credential").first()
    if dataset is None or not dataset.credential:
        return
    if _credential_needed_by_others(company, share.project, dataset.credential, dataset.id):
        return
    credential_share = access.find_share(company, "credential", dataset.credential, "projects", share.project)
    if credential_share is not None and credential_share.inherited:
        credential_share.delete()


def publish_run_datasets(task) -> List[Share]:
    """Shares the datasets the run used with the run's root project, and their credentials where decision 3 allows.
    Returns the shares created. Shares that already exist are left as they are."""
    run = Task.objects(company=task.company, id=task.id).only("id", "company", "user", "project").first()
    if run is None or not run.user or not run.project:
        return []
    if not user_access.is_active(run.user):
        return []
    root = user_access.root_project(run.company, run.project)
    owner = _root_owner(run.company, root) if root else None
    if root is None or not owner:
        return []

    created = []
    for dataset in _datasets_of(run.company, run.id):
        if not _run_user_can_use(run.company, run.user, dataset.id):
            continue
        if access.find_share(run.company, "dataset", dataset.id, "projects", root) is None:
            created.append(access.record_grant(run.company, owner, "dataset", dataset.id, "projects", root,
                                               level="read", actor=run.user, inherited=True, run_id=run.id))
        if dataset.credential:
            shared = _share_credential(run.company, run, root, dataset.credential)
            if shared is not None:
                created.append(shared)
    return created
