"""Use cases of the task field writes tasks.update, tasks.edit, tasks.create (issue 276) and tasks.update_batch
(issue 277), and of the model writes tasks.add_or_update_model and tasks.delete_models.

The endpoints in services/tasks.py decode the request, prepare the fields (the version rules and the tag conforming
live in the service layer, see issue 292), call one function here and shape the response. The business rules, the
write, and the bookkeeping after it (bll/project/project_changes.py) are here.

update, edit and the batch item are three modes with different rules. The differences are kept on purpose and are
named in the rule objects below; they are not unified. Each one is pinned by the characterisation tests of issues 268,
270 and 277 (the B, C, M and T series of tests/bll/task/test_task_batch_and_writers_characterisation.py).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from mongoengine import EmbeddedDocument
from pymongo import UpdateOne

from apiserver.apierrors import errors
from apiserver.bll.project import entity_access
from apiserver.bll.project.project_changes import (
    apply_effects,
    plan_task_batch,
    plan_task_create,
    plan_task_write,
)
from apiserver.bll.task.task_bll import TaskBLL
from apiserver.bll.task.utils import get_task_for_update, get_task_with_write_access
from apiserver.database.model.task.task import ModelItem, Task, TaskStatus
from apiserver.service_repo.auth import Identity

# Task.safe_update: filters by id and company; last_change comes in through its injected update.
SAFE_UPDATE = "safe_update"
# task.update(upsert=False): filters by the primary key; the task was read in company scope before.
TASK_UPDATE = "task_update"
# Collection.bulk_write of one $set per item: no mongoengine transform, so no stripping and no length or choice checks.
BULK_RAW = "bulk_raw"


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

# tasks.update_batch, one item per task: reads id, project and user like update. Differences from UPDATE_RULE, all kept
# as they are and pinned by the B series of the characterisation tests (issue 277): the raw bulk write (BULK_RAW), so
# the name is not stripped and the name and display name length checks do not run; the modified count is returned, not
# the matched one; no repository is recorded; and the tag cache and project time follow plan_task_batch.
# Suspect behaviours, decided in the decision issue (#282), not changed here.
BATCH_ITEM_RULE = WriteRule(
    endpoint="tasks.update_batch",
    fetch_only=("id", "project", "user"),
    status_gate=False,
    stamp_last_update=False,
    mechanism=BULK_RAW,
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

    if not fields:
        return WriteOutcome(updated=0, fields=None)

    now = datetime.now(timezone.utc)
    last_change = dict(last_change=now, last_changed_by=ctx.identity.user)
    if rule.stamp_last_update and not set(fields).issubset(Task.user_set_allowed()):
        last_change.update(last_update=now)
    fields.update(**last_change)

    updated = task.update(upsert=False, **fields)
    apply_effects(
        ctx.company_id,
        ctx.identity.user,
        plan_task_write(task.project, fields, updated),
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


def update_task_batch(
    ctx: WriteContext,
    items: Sequence[dict],
    prepare: Callable[[dict], dict],
) -> int:
    """tasks.update_batch (BATCH_ITEM_RULE). `prepare` turns one item into the partial update dict (the service layer's
    rules). All access and destination checks run before the bulk write, so a failure changes nothing. Returns the number
    of modified documents."""
    rule = BATCH_ITEM_RULE
    by_id = {i["task"]: i for i in items}
    entity_access.assert_tasks_access(ctx.company_id, ctx.identity, list(by_id), "write")
    tasks = {
        t.id: t
        for t in entity_access.assert_writable_tasks(
            identity=ctx.identity,
            company_id=ctx.company_id,
            ids=list(by_id),
            only=rule.fetch_only,
        )
    }

    if len(tasks) < len(by_id):
        missing = tuple(set(by_id).difference(tasks))
        raise errors.bad_request.InvalidTaskId(ids=missing)

    now = datetime.now(timezone.utc)
    bulk_ops = []
    updated_projects = set()
    for id, data in by_id.items():
        task = tasks[id]
        fields = prepare(data)
        # Checked for every item before the bulk write below, so that a failure changes nothing
        entity_access.assert_destination_project(ctx.identity, task, fields)
        partial_update_dict = Task.get_safe_update_dict(fields)
        if not partial_update_dict:
            continue
        partial_update_dict.update(
            last_change=now,
            last_changed_by=ctx.identity.user,
        )
        bulk_ops.append(UpdateOne({"_id": id, "company": ctx.company_id}, {"$set": partial_update_dict}))

        new_project = partial_update_dict.get("project", task.project)
        if new_project != task.project:
            updated_projects.update({new_project, task.project})
        elif any(f in partial_update_dict for f in ("tags", "system_tags")):
            updated_projects.add(task.project)

    updated = 0
    if bulk_ops:
        updated = Task._get_collection().bulk_write(bulk_ops).modified_count

    if updated and updated_projects:
        apply_effects(ctx.company_id, ctx.identity.user, plan_task_batch(updated_projects), task=None)
    return updated


def add_or_update_task_model(
    ctx: WriteContext,
    task_id: str,
    model_type: str,
    name: str,
    model: str,
    iteration: Optional[int],
) -> int:
    """tasks.add_or_update_model: sets the model of the name in place when it exists, pushes it otherwise, then updates
    the statistics. Returns the count of the statistics update, as the endpoint always returned."""
    get_task_for_update(company_id=ctx.company_id, task_id=task_id, force=True, identity=ctx.identity)

    models_field = f"models__{model_type}"
    item = ModelItem(name=name, model=model, updated=datetime.now(timezone.utc))
    query = {"id": task_id, f"{models_field}__name": name}
    updated = Task.objects(**query).update_one(**{f"set__{models_field}__S": item})

    return TaskBLL.update_statistics(
        task_id=task_id,
        company_id=ctx.company_id,
        user_id=ctx.identity.user,
        last_iteration_max=iteration,
        **({f"push__{models_field}": item} if not updated else {}),
    )


def delete_task_models(ctx: WriteContext, task_id: str, names_by_type: dict) -> int:
    """tasks.delete_models: pulls the names of each model type from the task. `names_by_type` maps a model type to the
    names to remove. Returns the matched count, as the endpoint always returned."""
    task = get_task_for_update(company_id=ctx.company_id, task_id=task_id, force=True, identity=ctx.identity)

    commands = {f"pull__models__{field}__name__in": names for field, names in names_by_type.items() if names}
    return task.update(
        last_change=datetime.now(timezone.utc),
        last_changed_by=ctx.identity.user,
        **commands,
    )
