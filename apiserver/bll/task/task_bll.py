from datetime import datetime, timezone
from typing import Callable, Collection, Sequence, Tuple, Optional, Dict, Any, List

import six
from mongoengine import Q
from redis import StrictRedis
from six import string_types

import apiserver.database.utils as dbutils
from apiserver.apierrors import errors, APIError
from apiserver.bll.queue import QueueBLL
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.bll.project import ProjectBLL
from apiserver.bll.util import update_project_time
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.metrics import EventStats, MetricEventStats
from apiserver.database.model.task.task import (
    Task,
    TaskStatus,
    Script,
)
from apiserver.database.model.queue import Queue
from apiserver.database.utils import (
    get_company_or_none_constraint,
    id as create_id,
    get_fields_attr,
)
from apiserver.es_factory import es_factory
from apiserver.redis_manager import redman
from apiserver.services.utils import validate_tags
from apiserver.service_repo.auth import Identity
from apiserver.utilities.parameter_key_escaper import mongoengine_safe
from .clone import (
    ClonePieces,
    CloneSpec,
    apply_execution_overrides,
    build_cloned_task,
    drop_queue_and_output_artifacts,
    merged_params,
    resolve_target_project,
    script_with_overrides,
    stamped_input_models,
    tags_to_register,
)
from .param_utils import params_prepare_for_save
from .utils import (
    ChangeStatusRequest,
    deleted_prefix,
    get_last_metric_updates,
    update_task,
    get_task_for_update,
)

log = config.logger(__file__)
org_bll = OrgBLL()
queue_bll = QueueBLL()
project_bll = ProjectBLL()


def folder_root_id(start_id: str, lookup: Callable[[str], Optional[Any]]) -> str:
    """
    The root of the parent chain that starts at start_id: the task reached by following parents until a task has no
    live parent. lookup(task_id) returns the task (with its parent) or None when it cannot be read. A missing task, a
    deleted parent or a loop stops the walk at the last readable task, so the result always names a task that exists.
    """
    current_id = start_id
    visited = {start_id}
    while True:
        task = lookup(current_id)
        parent_id = getattr(task, "parent", None) if task is not None else None
        if not parent_id or parent_id.startswith(deleted_prefix) or parent_id in visited:
            return current_id
        if lookup(parent_id) is None:
            return current_id
        visited.add(parent_id)
        current_id = parent_id


class TaskBLL:
    def __init__(self, events_es=None, redis=None):
        self.events_es = events_es or es_factory.connect("events")
        self.redis: StrictRedis = redis or redman.connection("apiserver")

    @staticmethod
    def get_by_id(
        company_id,
        task_id,
        required_status=None,
        only_fields=None,
        allow_public=False,
    ):
        if only_fields:
            if isinstance(only_fields, string_types):
                only_fields = [only_fields]
            else:
                only_fields = list(only_fields)
            only_fields = only_fields + ["status"]

        tasks = Task.get_many(
            company=company_id,
            query=Q(id=task_id),
            allow_public=allow_public,
            override_projection=only_fields,
            return_dicts=False,
        )
        task = None if not tasks else tasks[0]

        if not task:
            raise errors.bad_request.InvalidTaskId(id=task_id)

        if required_status and not task.status == required_status:
            raise errors.bad_request.InvalidTaskStatus(expected=required_status)

        return task

    @classmethod
    def edit_runtime(
        cls,
        company_id: str,
        identity: Identity,
        task_id: str,
        add_or_update: Dict[str, Any],
        remove: List[str],
        force: bool,
    ) -> int:
        task = get_task_for_update(company_id=company_id, task_id=task_id, force=force, identity=identity)

        update_cmds = {
            **{f"set__runtime__{mongoengine_safe(name)}": value for name, value in add_or_update.items()},
            **{f"unset__runtime__{mongoengine_safe(name)}": 1 for name in remove},
        }

        if not update_cmds:
            return 0

        return update_task(task, user_id=identity.user, update_cmds=update_cmds)

    @staticmethod
    def assert_exists(
        company_id, task_ids, only=None, allow_public=False, return_tasks=True
    ) -> Optional[Sequence[Task]]:
        task_ids = [task_ids] if isinstance(task_ids, six.string_types) else task_ids
        with translate_errors_context():
            ids = set(task_ids)
            q = Task.get_many(
                company=company_id,
                query=Q(id__in=ids),
                allow_public=allow_public,
                return_dicts=False,
            )
            if only:
                # Make sure to reset fields filters (some fields are excluded by default) since this
                # is an internal call and specific fields were requested.
                q = q.all_fields().only(*only)

            if q.count() != len(ids):
                raise errors.bad_request.InvalidTaskId(ids=task_ids)

            if return_tasks:
                return list(q)

    @staticmethod
    def create(company: str, user: str, fields: dict):
        now = datetime.now(timezone.utc)
        return Task(
            id=create_id(),
            user=user,
            company=company,
            created=now,
            last_update=now,
            last_change=now,
            last_changed_by=user,
            **fields,
        )

    @staticmethod
    def validate_input_models(task, allow_only_public=False):
        if not task.models.input:
            return

        company = None if allow_only_public else task.company
        model_ids = set(m.model for m in task.models.input)
        models = Model.objects(Q(id__in=model_ids) & get_company_or_none_constraint(company)).only("id")
        missing = model_ids - {m.id for m in models}
        if missing:
            raise errors.bad_request.InvalidModelId(models=missing)

        return

    task_script_stripped_fields = set([f for f, v in get_fields_attr(Script, "strip").items() if v])

    @classmethod
    def strip_script_fields(cls, script: dict):
        """
        Strip all script fields (remove leading and trailing whitespace chars) to avoid unusable names and paths
        """
        for field in cls.task_script_stripped_fields:
            value = script.get(field)
            if isinstance(value, str):
                script[field] = value.strip()

    @classmethod
    def clone_task(
        cls, company_id: str, user_id: str, identity: Identity, spec: CloneSpec
    ) -> Tuple[Task, Optional[dict]]:
        validate_tags(spec.tags, spec.system_tags)
        task: Task = cls.get_by_id(company_id=company_id, task_id=spec.task_id, allow_public=True)

        params_dict = merged_params(task, spec)
        now = datetime.now(timezone.utc)
        input_models = stamped_input_models(spec.input_models, now)
        script_dict = script_with_overrides(task, spec.script_overrides, cls.strip_script_fields)

        execution_dict = task.execution.to_proper_dict() if task.execution else {}
        execution_dict, input_models, container, legacy_params = apply_execution_overrides(
            execution_dict, spec.execution_overrides, input_models, spec.container, now
        )
        if legacy_params is not None:
            params_dict["execution"] = legacy_params

        params_prepare_for_save(params_dict, previous_task=task)
        drop_queue_and_output_artifacts(execution_dict)

        project, new_project_data = resolve_target_project(spec, company_id, user_id, identity)
        pieces = ClonePieces(
            project=project,
            parent=cls._clone_parent(company_id, task, spec.parent),
            script=script_dict,
            execution=execution_dict,
            input_models=input_models,
            container=container,
            params=params_dict,
        )
        new_task = build_cloned_task(task, spec, pieces, company_id, user_id, now)
        cls.validate(
            new_task,
            validate_models=spec.validate_references or input_models,
            validate_parent=spec.validate_references or spec.parent,
            validate_project=spec.validate_references or spec.project,
        )
        new_task.save()

        updated_tags, updated_system_tags = tags_to_register(task, new_task, spec)
        org_bll.update_tags(
            company_id,
            Tags.Task,
            projects=[new_task.project],
            tags=updated_tags,
            system_tags=updated_system_tags,
        )
        update_project_time(new_task.project)

        return new_task, new_project_data

    @classmethod
    def _clone_parent(cls, company_id: str, task: Task, parent: Optional[str]) -> str:
        """A clone is filed under the root of its source's folder, not under the source itself: the folder is named
        after the root, and a folder can only be read through a root that is in the same project."""

        def readable_task(task_id: str):
            try:
                return cls.get_by_id(
                    company_id=company_id,
                    task_id=task_id,
                    only_fields=["id", "parent"],
                    allow_public=True,
                )
            except errors.bad_request.InvalidTaskId:
                return None

        return folder_root_id(parent or task.id, readable_task)

    @classmethod
    def validate(
        cls,
        task: Task,
        validate_models=True,
        validate_parent=True,
        validate_project=True,
    ):
        """
        Validate task properties according to the flag
        Task project is always checked for being writable
        in order to disable the modification of public projects
        """
        if (
            validate_parent
            and task.parent
            and not task.parent.startswith(deleted_prefix)
            and not Task.get(company=task.company, id=task.parent, _only=("id",), include_public=True)
        ):
            raise errors.bad_request.InvalidTaskId("invalid parent", parent=task.parent)

        if task.project:
            project = Project.get_for_writing(company=task.company, id=task.project)
            if validate_project and not project:
                raise errors.bad_request.InvalidProjectId(id=task.project)

        if validate_models:
            cls.validate_input_models(task)

    @staticmethod
    def set_last_update(
        task_ids: Collection[str],
        company_id: str,
        user_id: str,
        last_update: datetime,
        **extra_updates,
    ):
        tasks = Task.objects(id__in=task_ids, company=company_id).only("status", "started")
        count = 0
        for task in tasks:
            updates = extra_updates
            if task.status == TaskStatus.in_progress and task.started:
                updates = {
                    "active_duration": (datetime.utcnow() - task.started).total_seconds(),
                    **extra_updates,
                }
            count += Task.objects(id=task.id, company=company_id).update(
                upsert=False,
                last_update=last_update,
                last_change=last_update,
                last_changed_by=user_id,
                **updates,
            )
        return count

    @staticmethod
    def update_statistics(
        task_id: str,
        company_id: str,
        user_id: str,
        last_update: datetime = None,
        last_iteration: int = None,
        last_iteration_max: int = None,
        last_scalar_events: Dict[str, Dict[str, dict]] = None,
        last_events: Dict[str, Dict[str, dict]] = None,
        **extra_updates,
    ):
        """
        Update task statistics
        :param task_id: Task's ID.
        :param company_id: Task's company ID.
        :param last_update: Last update time. If not provided, defaults to datetime.utcnow().
        :param last_iteration: Last reported iteration. Use this to set a value regardless of current
            task's last iteration value.
        :param last_iteration_max: Last reported iteration. Use this to conditionally set a value only
            if the current task's last iteration value is smaller than the provided value.
        :param last_scalar_events: Last reported metrics summary for scalar events (value, metric, variant).
        :param last_events: Last reported metrics summary (value, metric, event type).
        :param extra_updates: Extra task updates to include in this update call.
        :return:
        """
        last_update = last_update or datetime.now(timezone.utc)

        if last_iteration is not None:
            extra_updates.update(last_iteration=last_iteration)
        elif last_iteration_max is not None:
            extra_updates.update(max__last_iteration=last_iteration_max)

        raw_updates = {}
        if last_scalar_events is not None:
            get_last_metric_updates(
                task_id=task_id,
                last_scalar_events=last_scalar_events,
                raw_updates=raw_updates,
                extra_updates=extra_updates,
            )

        if last_events is not None:

            def events_per_type(metric_data_: Dict[str, dict]) -> Dict[str, EventStats]:
                return {
                    event_type: EventStats(last_update=event["timestamp"]) for event_type, event in metric_data_.items()
                }

            metric_stats = {
                dbutils.hash_field_name(metric_key): MetricEventStats(
                    metric=metric_key, event_stats_by_type=events_per_type(metric_data)
                )
                for metric_key, metric_data in last_events.items()
            }
            extra_updates["metric_stats"] = metric_stats

        ret = TaskBLL.set_last_update(
            task_ids=[task_id],
            company_id=company_id,
            user_id=user_id,
            last_update=last_update,
            **extra_updates,
        )
        if ret and raw_updates:
            Task.objects(id=task_id).update_one(__raw__=[{"$set": raw_updates}])

        return ret

    @staticmethod
    def remove_task_from_all_queues(company_id: str, task_id: str, exclude: str = None) -> int:
        more = {}
        if exclude:
            more["id__ne"] = exclude
        return Queue.objects(company=company_id, entries__task=task_id, **more).update(
            pull__entries__task=task_id, last_update=datetime.now(timezone.utc)
        )

    @classmethod
    def dequeue_and_change_status(
        cls,
        task: Task,
        company_id: str,
        user_id: str,
        status_message: str,
        status_reason: str,
        remove_from_all_queues=False,
        new_status=None,
        new_status_for_aborted_task=None,
    ):
        try:
            cls.dequeue(task, company_id=company_id, user_id=user_id, silent_fail=True)
        except APIError:
            # dequeue may fail if the queue was deleted
            pass

        if remove_from_all_queues:
            cls.remove_task_from_all_queues(company_id=company_id, task_id=task.id)

        if task.status not in [TaskStatus.queued, TaskStatus.in_progress]:
            return {"updated": 0}

        if new_status_for_aborted_task and task.status == TaskStatus.in_progress:
            new_status = new_status_for_aborted_task

        return ChangeStatusRequest(
            task=task,
            new_status=new_status or task.enqueue_status or TaskStatus.created,
            status_reason=status_reason,
            status_message=status_message,
            user_id=user_id,
            force=True,
        ).execute(enqueue_status=None)

    @classmethod
    def dequeue(cls, task: Task, company_id: str, user_id: str, silent_fail=False):
        """
        Dequeue the task from the queue
        :param task: task to dequeue
        :param company_id: task's company ID.
        :param silent_fail: do not throw exceptions. APIError is still thrown
        :raise errors.bad_request.InvalidTaskId: if the task's status is not queued
        :raise errors.bad_request.MissingRequiredFields: if the task is not queued
        :raise APIError or errors.server_error.TransactionError: if internal call to queues.remove_task fails
        :return: the result of queues.remove_task call. None in case of silent failure
        """
        if task.status not in (TaskStatus.queued,):
            if silent_fail:
                return
            raise errors.bad_request.InvalidTaskId(status=task.status, expected=TaskStatus.queued)

        if not task.execution or not task.execution.queue:
            if silent_fail:
                return
            raise errors.bad_request.MissingRequiredFields("task has no queue value", field="execution.queue")

        return {
            "removed": queue_bll.remove_task(
                company_id=company_id,
                user_id=user_id,
                queue_id=task.execution.queue,
                task_id=task.id,
            )
        }
