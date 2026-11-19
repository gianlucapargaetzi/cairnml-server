"""Use cases of the task field writes tasks.update, tasks.edit and tasks.create (issue 276).

The endpoints in services/tasks.py decode the request, prepare the fields (the version rules and the tag conforming
live in the service layer, see issue 292), call one function here and shape the response. The business rules, the
write, and the bookkeeping after it (bll/project/project_changes.py) are here.

update and edit are two modes with different rules. The differences are kept on purpose and are named in the rule
objects below; they are not unified. Each one is pinned by the characterisation tests of issues 268 and 270.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from mongoengine import EmbeddedDocument
from mongoengine.queryset.transform import COMPARISON_OPERATORS

from apiserver.apierrors import errors
from apiserver.bll.project import entity_access
from apiserver.bll.project.project_changes import apply_effects, plan_task_create, plan_task_write
from apiserver.bll.task.task_bll import TaskBLL
from apiserver.bll.task.utils import get_task_with_write_access
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.service_repo.auth import Identity

# Task.safe_update: filters by id and company; last_change comes in through its injected update.
SAFE_UPDATE = "safe_update"
# task.update(upsert=False): filters by the primary key; the task was read in company scope before.
TASK_UPDATE = "task_update"


@dataclass(frozen=True)
class WriteRule:
    endpoint: str
    # The projection the task is read with. None reads the whole task.
    fetch_only: Optional[tuple]
    # edit refuses a task that is not in status created, unless forced. update has no such gate.
    status_gate: bool
    # edit also stamps last_update when a written field is not a user-set field. update stamps last_change only.
    stamp_last_update: bool
    # The write mechanism, kept as it is (see SAFE_UPDATE and TASK_UPDATE).
    mechanism: str


# tasks.update: reads only id, project and user; writes through Task.safe_update.
UPDATE_RULE = WriteRule(
    endpoint="tasks.update",
    fetch_only=("id", "project", "user"),
    status_gate=False,
    stamp_last_update=False,
    mechanism=SAFE_UPDATE,
)

# tasks.edit: reads the whole task; writes through task.update and merges embedded documents first.
EDIT_RULE = WriteRule(
    endpoint="tasks.edit",
    fetch_only=None,
    status_gate=True,
    stamp_last_update=True,
    mechanism=TASK_UPDATE,
)


@dataclass(frozen=True)
class WriteContext:
    company_id: str
    identity: Identity


@dataclass(frozen=True)
class WriteOutcome:
    updated: int
    # The fields as they were written, with the stamps. None when nothing was written and the response has no fields.
    fields: Optional[dict]


def update_task_fields(
    ctx: WriteContext,
    task_id: str,
    prepare: Callable[[Task], dict],
) -> WriteOutcome:
    """tasks.update. `prepare` turns the request into the partial update dict (the service layer's rules)."""
    rule = UPDATE_RULE
    task = get_task_with_write_access(
        task_id=task_id,
        company_id=ctx.company_id,
        identity=ctx.identity,
        only=rule.fetch_only,
    )

    partial_update_dict = prepare(task)
    if not partial_update_dict:
        return WriteOutcome(updated=0, fields=None)

    entity_access.assert_destination_project(ctx.identity, task, partial_update_dict)

    updated_count, updated_fields = Task.safe_update(
        company_id=ctx.company_id,
        id=task_id,
        partial_update_dict=partial_update_dict,
        injected_update=dict(
            last_change=datetime.now(timezone.utc),
            last_changed_by=ctx.identity.user,
        ),
    )
    apply_effects(
        ctx.company_id,
        ctx.identity.user,
        plan_task_write(task.project, updated_fields, updated_count),
        task_id,
    )
    return WriteOutcome(updated=updated_count, fields=updated_fields)


def edit_task_fields(
    ctx: WriteContext,
    task_id: str,
    prepare: Callable[[Task], dict],
    force: bool,
) -> WriteOutcome:
    """tasks.edit. `prepare` turns the request into the fields of the edit (the service layer's rules), given the task
    because the edit rules read its output and its previous execution parameters."""
    rule = EDIT_RULE
    task = get_task_with_write_access(
        task_id=task_id,
        company_id=ctx.company_id,
        identity=ctx.identity,
        only=rule.fetch_only,
    )

    if rule.status_gate and not force and task.status != TaskStatus.created:
        raise errors.bad_request.InvalidTaskStatus(expected=TaskStatus.created, status=task.status)

    fields = prepare(task)

    entity_access.assert_destination_project(ctx.identity, task, fields)

    for key in fields:
        field = getattr(task, key, None)
        value = fields[key]
        if field and isinstance(value, dict) and isinstance(field, EmbeddedDocument):
            d = field.to_mongo(use_db_field=False).to_dict()
            d.update(value)
            fields[key] = d

    TaskBLL.validate(TaskBLL.create(company=ctx.identity.company, user=ctx.identity.user, fields=fields))

    # make sure field names do not end in mongoengine comparison operators
    fixed_fields = {(k if k not in COMPARISON_OPERATORS else "%s__" % k): v for k, v in fields.items()}
    if not fixed_fields:
        return WriteOutcome(updated=0, fields=None)

    now = datetime.now(timezone.utc)
    last_change = dict(last_change=now, last_changed_by=ctx.identity.user)
    if rule.stamp_last_update and not set(fields).issubset(Task.user_set_allowed()):
        last_change.update(last_update=now)
    fields.update(**last_change)
    fixed_fields.update(**last_change)

    updated = task.update(upsert=False, **fixed_fields)
    apply_effects(
        ctx.company_id,
        ctx.identity.user,
        plan_task_write(task.project, fixed_fields, updated),
        task_id,
    )
    return WriteOutcome(updated=updated, fields=fields)


def save_new_task(ctx: WriteContext, task: Task, fields: dict) -> None:
    """tasks.create: the task is already built and validated by the service layer. Saves it and does its bookkeeping."""
    task.save()
    apply_effects(
        ctx.company_id,
        ctx.identity.user,
        plan_task_create(task.project, fields),
        task,
    )
