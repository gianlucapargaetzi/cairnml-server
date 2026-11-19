"""
Project statistics: the part that needs no database.

The functions here build the aggregation pipelines and the query objects, and shape the results that the database
returned. None of them reads or writes the database, so they are tested without one. ProjectBLL keeps the thin
database calls (the aggregations, the child lookups and the user names) and passes the data through these functions.
"""

from datetime import datetime, timedelta
from functools import reduce
from itertools import chain
from operator import itemgetter
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, TypeVar

from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.apimodels.projects import ProjectChildrenType
from apiserver.database.model import EntityVisibility
from apiserver.database.model.base import GetMixin
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, TaskStatus
from apiserver.database.utils import get_company_or_none_constraint, get_options

reports_tag = "reports"
dataset_tag = "dataset"
pipeline_tag = "pipeline"
datasets_project_name = ".datasets"
pipelines_project_name = ".pipelines"

TASK_COUNT_STATUSES = get_options(TaskStatus)
ARCHIVED = EntityVisibility.archived.value
NON_ARCHIVED = f"-{EntityVisibility.archived.value}"

T = TypeVar("T")
ChildProjects = Mapping[str, Sequence[Any]]


# --- filters -------------------------------------------------------------------------------------------------------


def with_system_tag(filter_: Optional[Mapping[str, Any]], tag: str) -> dict:
    """
    A copy of the filter with the tag among its system tags. The filter passed in is not changed.
    """
    new_filter = dict(filter_ or {})
    tags = new_filter.get("system_tags")
    tags = list(tags) if isinstance(tags, list) else []
    if tag not in tags:
        tags.append(tag)
    new_filter["system_tags"] = tags
    return new_filter


def with_non_archived(filter_: Optional[Mapping[str, Any]]) -> dict:
    """
    A copy of the filter that leaves archived entities out, unless the filter names the archived state itself.
    """
    new_filter = dict(filter_ or {})
    system_tags_filter = new_filter.get("system_tags", [])
    if not any(t in system_tags_filter for t in (ARCHIVED, NON_ARCHIVED)):
        new_filter["system_tags"] = [NON_ARCHIVED, *system_tags_filter]
    return new_filter


def excluding_archived(filter_: Optional[Mapping[str, Any]]) -> dict:
    """
    A copy of the filter that always leaves archived tasks out, whatever archived state the filter names.
    """
    filter_ = filter_ or {}
    new_filter = {k: v for k, v in filter_.items() if k != "system_tags"}
    system_tags = [tag for tag in filter_.get("system_tags", []) if tag not in (ARCHIVED, NON_ARCHIVED)]
    system_tags.append(NON_ARCHIVED)
    new_filter["system_tags"] = system_tags
    return new_filter


def match_conditions(
    company: str,
    project_ids: Sequence[str],
    filter_: Optional[Mapping[str, Any]],
    users: Optional[Sequence[str]],
    project_field: str = "project",
) -> dict:
    """
    The $match condition of an aggregation over the entities of the given projects, with the list and dictionary
    filters translated to the database query language.
    """
    conditions = {
        "company": {"$in": ["", company]},
        project_field: {"$in": project_ids},
    }
    if users:
        conditions["user"] = {"$in": users}

    if not filter_:
        return conditions

    or_conditions = []
    for field, field_filter in filter_.items():
        if not (field_filter and isinstance(field_filter, (list, dict))):
            raise errors.bad_request.ValidationError(f"Non empty list or dictionary expected for the field: {field}")

        if isinstance(field_filter, list):
            if not all(isinstance(t, str) for t in field_filter):
                raise errors.bad_request.ValidationError(f"Only string values are allowed in the list filter: {field}")
            helper = GetMixin.NewListFieldBucketHelper(field, data=field_filter, legacy=True)
            op = helper.global_operator
            db_query = {op: helper.actions}
        else:
            helper = GetMixin.ListQueryFilter.from_data(field, field_filter)
            db_query = helper.db_query

        for op, actions in db_query.items():
            field_conditions = {}
            for action, values in actions.items():
                value = list(set(values)) if isinstance(values, list) else values
                for key in reversed(action.split("__")):
                    value = {f"${key}": value}
                field_conditions.update(value)

            if op == Q.OR and len(field_conditions) > 1:
                or_conditions.append(
                    {"$or": [{field: {db_modifier: cond}} for db_modifier, cond in field_conditions.items()]}
                )
            else:
                conditions[field] = field_conditions

    if or_conditions:
        if len(or_conditions) == 1:
            conditions.update(next(iter(or_conditions)))
        else:
            # noinspection PyTypeChecker
            conditions["$and"] = [c for c in or_conditions]

    return conditions


# --- pipelines -----------------------------------------------------------------------------------------------------


def count_by_field_pipeline(match: Mapping[str, Any], field: str) -> list:
    """
    Number of the matched entities per value of the field (the project, or the parent of a project)
    """
    return [
        {"$match": match},
        {"$project": {field: 1}},
        {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
    ]


def dataset_pipeline(match: Mapping[str, Any], project_field: str) -> list:
    """
    Number of the matched datasets per project, and the tags of each project's datasets
    """
    return [
        {"$match": match},
        {"$project": {project_field: 1, "tags": 1}},
        {
            "$group": {
                "_id": f"${project_field}",
                "count": {"$sum": 1},
                "tags": {"$push": "$tags"},
            }
        },
    ]


def dataset_runtime_pipeline(company: str, project_ids: Sequence[str], users: Optional[Sequence[str]]) -> list:
    """
    The runtime of the latest non archived task of each project that reported dataset runtime
    """
    return [
        {
            "$match": {
                **match_conditions(
                    company=company,
                    project_ids=project_ids,
                    users=users,
                    filter_={"system_tags": [NON_ARCHIVED]},
                ),
                "runtime": {"$exists": True, "$gt": {}},
            }
        },
        {"$project": {"project": 1, "runtime": 1, "last_update": 1}},
        {"$sort": {"project": 1, "last_update": 1}},
        {"$group": {"_id": "$project", "runtime": {"$last": "$runtime"}}},
    ]


def project_runtime_pipeline(
    company: str,
    project_ids: Sequence[str],
    filter_: Optional[Mapping[str, Any]],
    users: Optional[Sequence[str]],
    time_thresh: datetime,
) -> list:
    """
    The run time and the task counts per project, for the non archived tasks of the matched projects
    """
    return [
        {
            "$match": match_conditions(
                company=company,
                project_ids=project_ids,
                filter_=excluding_archived(filter_),
                users=users,
            )
        },
        {
            "$project": {
                "project": 1,
                "status": 1,
                "started": 1,
                "completed": 1,
            }
        },
        {
            "$group": {
                "_id": "$project",
                "total_runtime": _runtime_subquery(),
                "recently_completed": _completed_after_subquery(time_thresh=time_thresh),
                "max_task_started": {"$max": "$started"},
                **_task_status_counts_subquery(),
            }
        },
    ]


def _task_status_counts_subquery() -> dict:
    def get_status_condition(status) -> dict:
        if status == TaskStatus.unknown:
            return {"$eq": [{"$ifNull": ["$status", status]}, status]}

        return {"$eq": ["$status", status]}

    return {
        status: {
            "$sum": {
                "$cond": {
                    "if": get_status_condition(status),
                    "then": 1,
                    "else": 0,
                }
            }
        }
        for status in TASK_COUNT_STATUSES
    }


def _completed_after_subquery(time_thresh: datetime) -> dict:
    return {
        "$sum": {
            "$cond": {
                "if": {
                    "$and": [
                        "$completed",
                        {"$gt": ["$completed", time_thresh]},
                        {
                            "$not": {
                                "$in": [
                                    "$status",
                                    [
                                        TaskStatus.queued,
                                        TaskStatus.in_progress,
                                        TaskStatus.failed,
                                    ],
                                ]
                            }
                        },
                    ]
                },
                "then": 1,
                "else": 0,
            }
        }
    }


def _runtime_subquery() -> dict:
    return {
        "$sum": {
            "$cond": {
                "if": {
                    "$and": [
                        "$started",
                        "$completed",
                        {"$gt": ["$completed", "$started"]},
                    ]
                },
                "then": {
                    "$floor": {
                        "$divide": [
                            {"$subtract": ["$completed", "$started"]},
                            1000.0,
                        ]
                    }
                },
                "else": 0,
            }
        }
    }


def contributions_pipeline(
    company: str,
    project_ids: Sequence[str],
    period_start: datetime,
    utc_offset_minutes: int,
) -> list:
    """
    The runs of the projects created since the period start, faceted by local day, by status and by user
    """
    match = {
        "company": {"$in": list({"", company})},
        "project": {"$in": list(project_ids)},
        "system_tags": {"$ne": EntityVisibility.hidden.value},
        "created": {"$gte": period_start},
    }
    tz = utc_offset_suffix(utc_offset_minutes)
    return [
        {"$match": match},
        {
            "$facet": {
                "days": [
                    {
                        "$group": {
                            "_id": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": "$created",
                                    "timezone": tz,
                                }
                            },
                            "runs": {"$sum": 1},
                        }
                    },
                    {"$sort": {"_id": 1}},
                ],
                "statuses": [
                    {"$group": {"_id": "$status", "runs": {"$sum": 1}}},
                ],
                "users": [
                    {"$group": {"_id": "$user", "runs": {"$sum": 1}}},
                    {"$sort": {"runs": -1, "_id": 1}},
                ],
            }
        },
    ]


# --- shaping of the results ----------------------------------------------------------------------------------------


def aggregate_project_data(
    func: Callable[[T, T], T],
    project_ids: Sequence[str],
    child_projects: ChildProjects,
    data: Mapping[str, T],
) -> Dict[str, T]:
    """
    Given a list of project ids and data collected over these projects and their subprojects
    For each project aggregates the data from all of its subprojects
    """
    aggregated = {}
    if not data:
        return aggregated
    for pid in project_ids:
        relevant_projects = {p.id for p in child_projects.get(pid, [])} | {pid}
        relevant_data = [data for p, data in data.items() if p in relevant_projects]
        if not relevant_data:
            continue
        aggregated[pid] = reduce(func, relevant_data)
    return aggregated


def children_info(project_ids: Sequence[str], child_projects: ChildProjects) -> Dict[str, list]:
    """
    The subprojects of each project as {id, name} pairs sorted by name
    """
    return {
        project: sorted(
            [{"id": c.id, "name": c.name} for c in child_projects.get(project, [])],
            key=itemgetter("name"),
        )
        for project in project_ids
    }


def dataset_stats_by_project(
    rows: Sequence[Mapping[str, Any]],
    project_ids: Sequence[str],
    child_projects: ChildProjects,
) -> Dict[str, dict]:
    """
    The dataset count and the sorted distinct tags of each project, subprojects included
    """
    project_stats = {
        row["_id"]: {
            "count": row.get("count", 0),
            "tags": set(chain.from_iterable(row.get("tags", []))),
        }
        for row in rows
    }

    def concat_dataset_stats(a: dict, b: dict) -> dict:
        return {
            "count": a.get("count", 0) + b.get("count", 0),
            "tags": a.get("tags", {}) | b.get("tags", {}),
        }

    top_project_stats = aggregate_project_data(
        func=concat_dataset_stats,
        project_ids=project_ids,
        child_projects=child_projects,
        data=project_stats,
    )
    for stat in top_project_stats.values():
        stat["tags"] = sorted(list(stat.get("tags", {})))

    stats = {}
    for project in project_ids:
        if project in top_project_stats:
            stats[project] = {"datasets": top_project_stats[project]}
        else:
            stats[project] = {"datasets": {"count": 0, "tags": []}}
    return stats


def dataset_runtime_by_project(rows: Sequence[Mapping[str, Any]]) -> Dict[str, dict]:
    """
    The file count and the total size reported by the latest dataset runtime of each project
    """
    return {
        row["_id"]: {
            "file_count": row["runtime"].get("ds_file_count", 0),
            "total_size": row["runtime"].get("ds_total_size", 0),
        }
        for row in rows
    }


def counts_by_project(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    return {row["_id"]: row["count"] for row in rows}


def own_count_by_project(project_ids: Sequence[str], counts: Mapping[str, int], name: str) -> Dict[str, dict]:
    return {pid: {name: counts.get(pid, 0)} for pid in project_ids}


def own_contents_by_project(
    project_ids: Sequence[str],
    tasks: Mapping[str, int],
    models: Mapping[str, int],
) -> Dict[str, dict]:
    return {pid: {"own_tasks": tasks.get(pid, 0), "own_models": models.get(pid, 0)} for pid in project_ids}


def sum_runtime(a: Mapping[str, Any], b: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Adds up the runtime counters of two projects. The latest task start is the maximum, not the sum.
    """
    ret = {}
    for key in set(a) | set(b):
        if key == "max_task_started":
            val = max(a.get(key) or datetime.min, b.get(key) or datetime.min)
        else:
            val = a.get(key, 0) + b.get(key, 0)

        ret[key] = val

    return ret


def runtime_by_project(rows: Sequence[Mapping[str, Any]]) -> Dict[str, dict]:
    return {row["_id"]: {k: v for k, v in row.items() if k != "_id"} for row in rows}


def active_stats_by_project(
    project_ids: Sequence[str],
    runtime: Mapping[str, dict],
    child_projects: ChildProjects,
) -> Dict[str, dict]:
    """
    The task counts, the run time and the last run of each project, its subprojects included
    """
    runtime = aggregate_project_data(
        func=sum_runtime,
        project_ids=project_ids,
        child_projects=child_projects,
        data=runtime,
    )

    def get_project_info(project_id):
        project_runtime = runtime.get(project_id, {})

        def get_time_or_none(value):
            return value if value != datetime.min else None

        status_counts = {status: project_runtime.get(status, 0) for status in TASK_COUNT_STATUSES}
        return {
            "status_count": status_counts,
            "total_tasks": sum(status_counts.values()),
            "total_runtime": project_runtime.get("total_runtime", 0),
            "completed_tasks_24h": project_runtime.get("recently_completed", 0),
            "last_task_run": get_time_or_none(project_runtime.get("max_task_started", datetime.min)),
        }

    return {project: {"active": get_project_info(project)} for project in project_ids}


def contributor_ids(result: Mapping[str, Any], limit: int) -> list:
    """
    The ids of the users shown as contributors, the first `limit` of the ranking
    """
    return [group["_id"] for group in result.get("users", [])[:limit] if group["_id"]]


def contributions_period_start(now: datetime, utc_offset_minutes: int, period_days: int) -> datetime:
    """
    The start, in UTC, of the first local day of a period of `period_days` local days ending with today
    """
    offset = timedelta(minutes=utc_offset_minutes)
    local_now = now + offset
    start_day = local_now.date() - timedelta(days=period_days - 1)
    return datetime(start_day.year, start_day.month, start_day.day) - offset


def utc_offset_suffix(utc_offset_minutes: int) -> str:
    """
    The offset as a timezone string, for example +02:00 or -03:30
    """
    sign = "+" if utc_offset_minutes >= 0 else "-"
    hours, minutes = divmod(abs(utc_offset_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def contributions_response(
    period_days: int,
    result: Mapping[str, Any],
    names: Mapping[str, str],
    limit: int,
) -> dict:
    statuses = {group["_id"]: group["runs"] for group in result.get("statuses", [])}
    running = sum(statuses.get(status, 0) for status in (TaskStatus.in_progress, TaskStatus.queued))
    users = result.get("users", [])
    return {
        "period_days": period_days,
        "total": sum(statuses.values()),
        "completed": statuses.get(TaskStatus.completed, 0),
        "failed": statuses.get(TaskStatus.failed, 0),
        "running": running,
        "days": [{"date": day["_id"], "runs": day["runs"]} for day in result.get("days", [])],
        "contributors": [
            {"id": group["_id"], "name": names.get(group["_id"], ""), "runs": group["runs"]} for group in users[:limit]
        ],
        "contributor_count": len(users),
    }


# --- selected children ---------------------------------------------------------------------------------------------


def selected_children_queries(
    company: str,
    users: Optional[Sequence[str]],
    allow_public: bool,
    children_type: Optional[ProjectChildrenType],
    children_tags: Optional[Sequence[str]],
    children_tags_filter: Optional[dict],
    project_ids: Optional[Sequence[str]],
    with_children: Callable[[Sequence[str]], Sequence[str]],
) -> Tuple[Optional[Q], Dict[type, Q]]:
    """
    The project query and the query per child class that find the projects with matching children.
    With project ids, the queries are restricted to these projects and their subprojects, which `with_children`
    returns (it is the only database step, and it is not called when the arguments are refused).
    """
    if not (users or children_type):
        raise errors.bad_request.ValidationError("Either active users or children_condition should be specified")

    query = get_company_or_none_constraint(company) if allow_public else Q(company=company)
    if users:
        query &= Q(user__in=users)

    project_query = None
    if children_tags_filter:
        child_query = query & GetMixin.get_list_filter_query("tags", children_tags_filter)
    elif children_tags:
        child_query = query & GetMixin.get_list_field_query("tags", children_tags)
    else:
        child_query = query

    if children_type == ProjectChildrenType.dataset:
        child_queries = {Project: child_query & Q(system_tags__in=[dataset_tag], basename__ne=datasets_project_name)}
    elif children_type == ProjectChildrenType.pipeline:
        child_queries = {Project: child_query & Q(system_tags__in=[pipeline_tag], basename__ne=pipelines_project_name)}
    elif children_type == ProjectChildrenType.report:
        child_queries = {Task: child_query & Q(system_tags__in=[reports_tag])}
    else:
        project_query = query
        child_queries = {entity_cls: query for entity_cls in (Task, Model)}

    if project_ids:
        ids_with_children = with_children(project_ids)
        if project_query:
            project_query &= Q(id__in=ids_with_children)
        for child_cls in child_queries:
            child_queries[child_cls] &= (
                Q(parent__in=ids_with_children) if child_cls is Project else Q(project__in=ids_with_children)
            )

    return project_query, child_queries


def selected_ids_within(selected_project_ids: Sequence[str], project_ids: Optional[Sequence[str]]) -> list:
    """
    The selected projects, restricted to the requested project ids when any are requested
    """
    if project_ids:
        return list(set(selected_project_ids) & set(project_ids))
    return list(selected_project_ids)
