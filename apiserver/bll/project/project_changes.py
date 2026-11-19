"""Bookkeeping after a task is written: the task tag cache, the project time and the repository recording.

The plan functions are pure. They say which bookkeeping a write needs, and the per-endpoint differences are
kept as they are, one rule per call site (issue 276, section 2.2). `apply_effects` performs a plan.
"""

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from apiserver.bll import repository_runs
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.bll.util import update_project_time

org_bll = OrgBLL()


@dataclass(frozen=True)
class ProjectEffects:
    """What a write does after it saved. Every field that is None or empty means: no call."""

    # Projects whose task tag cache is reset (both tag fields): a task moved between projects
    reset_projects: Optional[tuple] = None
    # (project, written fields) whose task tag cache is invalidated for the fields that were written
    update_tags: Optional[tuple] = None
    # One update_project_time call per entry; an entry may be None, which the call treats as a no-op
    project_time: tuple = ()
    # One update_project_time call naming all these projects (tasks.update_batch); None means no such call
    project_time_together: Optional[tuple] = None
    # The task's repository is recorded for its project
    record_repository: bool = False
    # The repository of each of these tasks is recorded, one call per task in the given order (tasks.update_batch)
    record_repository_of: tuple = ()


def plan_task_create(project: Optional[str], fields: dict) -> ProjectEffects:
    """tasks.create: the tag cache of the task's project, its time, and the repository."""
    return ProjectEffects(
        update_tags=(project, fields),
        project_time=(project,),
        record_repository=True,
    )


def plan_task_write(old_project: Optional[str], written: dict, updated: int) -> ProjectEffects:
    """tasks.update and tasks.edit share this plan: `written` is the dict of the fields the write set, `updated` the
    count the write reported. Nothing changed means nothing to do.

    - a move (the written project differs from the old one) resets the tag cache of both projects, new first
    - otherwise only the written tag fields of the old project are invalidated
    - the project time is set for the new project only, and only when the write named a project (else None)
    """
    if not updated:
        return ProjectEffects()

    new_project = written.get("project", old_project)
    if new_project != old_project:
        return ProjectEffects(
            reset_projects=(new_project, old_project),
            project_time=(written.get("project"),),
            record_repository=True,
        )
    return ProjectEffects(
        update_tags=(old_project, written),
        project_time=(written.get("project"),),
        record_repository=True,
    )


def plan_task_batch(updated_projects: Iterable[Optional[str]]) -> ProjectEffects:
    """tasks.update_batch: every project a modified item moved to or from, or whose tags it changed, has its task tag
    cache reset (both tag fields) and its project time set in one call. The batch records no repository (see
    plan_task_batch_repositories). The caller plans only when something was modified (issue 277, B7-B13 of the
    characterisation tests)."""
    projects = tuple(updated_projects)
    return ProjectEffects(
        reset_projects=projects,
        project_time_together=projects,
    )


def plan_task_batch_repositories(written_task_ids: Iterable[str]) -> ProjectEffects:
    """tasks.update_batch: the repository of every written task is recorded, as tasks.update records it (issue 301).
    Kept apart from plan_task_batch, which keeps the tag cache and project time bookkeeping."""
    return ProjectEffects(record_repository_of=tuple(written_task_ids))


def reset_task_tags(company_id: str, projects: Sequence[Optional[str]]) -> None:
    org_bll.reset_tags(company_id, Tags.Task, projects=projects)


def update_cached_tags(company_id: str, project: Optional[str], fields: dict) -> None:
    org_bll.update_tags(
        company_id,
        Tags.Task,
        projects=[project],
        tags=fields.get("tags"),
        system_tags=fields.get("system_tags"),
    )


def record_repository(company_id: str, user: str, task) -> None:
    """The repository a saved task names is recorded for the task's project (bll/repository_runs.record_script); a failure
    never refuses the save. ``task`` is the task, or its id."""
    repository_runs.record_script(company_id, user, task)


def apply_effects(company_id: str, user: str, effects: ProjectEffects, task=None) -> None:
    """Performs a plan in the order the endpoints always used: tag cache, project time, repository. `task` is only
    needed when the plan records the repository."""
    if effects.reset_projects is not None:
        reset_task_tags(company_id, projects=list(effects.reset_projects))
    if effects.update_tags is not None:
        project, fields = effects.update_tags
        update_cached_tags(company_id, project=project, fields=fields)
    for project in effects.project_time:
        update_project_time(project)
    if effects.project_time_together is not None:
        update_project_time(project_ids=list(effects.project_time_together))
    if effects.record_repository:
        record_repository(company_id, user, task)
    for task_id in effects.record_repository_of:
        record_repository(company_id, user, task_id)
