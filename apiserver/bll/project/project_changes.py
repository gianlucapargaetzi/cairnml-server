"""Bookkeeping after a task is written: the task tag cache, the project time and the repository recording.

The plan functions are pure. They say which bookkeeping a write needs, and the per-endpoint differences are
kept as they are, one rule per call site (issue 276, section 2.2). `apply_effects` performs a plan.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

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
    # The task's repository is recorded for its project
    record_repository: bool = False


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


def apply_effects(company_id: str, user: str, effects: ProjectEffects, task) -> None:
    """Performs a plan in the order the endpoints always used: tag cache, project time, repository."""
    if effects.reset_projects is not None:
        reset_task_tags(company_id, projects=list(effects.reset_projects))
    if effects.update_tags is not None:
        project, fields = effects.update_tags
        update_cached_tags(company_id, project=project, fields=fields)
    for project in effects.project_time:
        update_project_time(project)
    if effects.record_repository:
        record_repository(company_id, user, task)
