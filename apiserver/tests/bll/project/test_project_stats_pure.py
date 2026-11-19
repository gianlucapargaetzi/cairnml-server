# Unit tests of the pure functions of bll/project/stats.py (tracking issue #267, step 10, issue #281).
# None of these tests reads or writes the database: they run without a MongoDB or Redis server.
from datetime import datetime
from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.projects import ProjectChildrenType
from apiserver.bll.project import stats
from apiserver.database.model import EntityVisibility
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task, TaskStatus

ARCHIVED = EntityVisibility.archived.value
NON_ARCHIVED = f"-{ARCHIVED}"
NOW = datetime(2026, 10, 30, 12, 0)


def child(id_, name=None):
    return SimpleNamespace(id=id_, name=name or id_)


# --- filters: none of them changes the filter it is given ----------------------------------------------------------


def test_with_system_tag_adds_the_tag_to_a_copy():
    original = {"system_tags": ["x"], "tags": ["t"]}

    result = stats.with_system_tag(original, "dataset")

    assert result == {"system_tags": ["x", "dataset"], "tags": ["t"]}
    assert original == {"system_tags": ["x"], "tags": ["t"]}
    assert result is not original
    assert result["system_tags"] is not original["system_tags"]


@pytest.mark.parametrize(
    "filter_, expected",
    [
        (None, {"system_tags": ["dataset"]}),
        ({}, {"system_tags": ["dataset"]}),
        ({"system_tags": ["dataset", "x"]}, {"system_tags": ["dataset", "x"]}),
        ({"system_tags": "x"}, {"system_tags": ["dataset"]}),
    ],
)
def test_with_system_tag_gives_the_same_effective_filter_in_every_shape(filter_, expected):
    assert stats.with_system_tag(filter_, "dataset") == expected


def test_with_non_archived_leaves_archived_out_unless_named():
    original = {"system_tags": ["x"]}

    assert stats.with_non_archived(original) == {"system_tags": [NON_ARCHIVED, "x"]}
    assert original == {"system_tags": ["x"]}
    assert stats.with_non_archived({"system_tags": [ARCHIVED]}) == {"system_tags": [ARCHIVED]}
    assert stats.with_non_archived({"system_tags": [NON_ARCHIVED]}) == {"system_tags": [NON_ARCHIVED]}
    assert stats.with_non_archived(None) == {"system_tags": [NON_ARCHIVED]}


def test_excluding_archived_replaces_any_archived_state_and_keeps_the_other_keys():
    original = {"system_tags": [ARCHIVED, "x"], "type": ["training"]}

    result = stats.excluding_archived(original)

    assert result == {"system_tags": ["x", NON_ARCHIVED], "type": ["training"]}
    assert original == {"system_tags": [ARCHIVED, "x"], "type": ["training"]}


# --- match conditions and pipelines ---------------------------------------------------------------------------------


def test_match_conditions_without_a_filter_names_the_company_and_the_projects():
    match = stats.match_conditions(company="c", project_ids=["p1"], filter_=None, users=None)

    assert match == {"company": {"$in": ["", "c"]}, "project": {"$in": ["p1"]}}


def test_match_conditions_translates_a_list_filter_and_users():
    match = stats.match_conditions(
        company="c",
        project_ids=["p1"],
        filter_={"system_tags": ["x"]},
        users=["u"],
        project_field="parent",
    )

    assert match == {
        "company": {"$in": ["", "c"]},
        "parent": {"$in": ["p1"]},
        "user": {"$in": ["u"]},
        "system_tags": {"$in": ["x"]},
    }


def test_match_conditions_refuses_a_list_filter_with_other_values():
    with pytest.raises(errors.bad_request.ValidationError):
        stats.match_conditions(company="c", project_ids=[], filter_={"tags": [1]}, users=None)


def test_count_by_field_pipeline_groups_by_the_field():
    pipeline = stats.count_by_field_pipeline({"m": 1}, field="parent")

    assert pipeline == [
        {"$match": {"m": 1}},
        {"$project": {"parent": 1}},
        {"$group": {"_id": "$parent", "count": {"$sum": 1}}},
    ]


def test_dataset_runtime_pipeline_needs_a_runtime_and_leaves_archived_out():
    pipeline = stats.dataset_runtime_pipeline("c", ["p1"], users=None)
    match = pipeline[0]["$match"]

    assert match["runtime"] == {"$exists": True, "$gt": {}}
    assert match["system_tags"] == {"$nin": [ARCHIVED]}
    assert pipeline[-1] == {"$group": {"_id": "$project", "runtime": {"$last": "$runtime"}}}


def test_project_runtime_pipeline_uses_the_given_time_threshold():
    threshold = datetime(2026, 10, 29, 12, 0)

    pipeline = stats.project_runtime_pipeline("c", ["p1"], None, None, time_thresh=threshold)

    group = pipeline[-1]["$group"]
    completed_after = group["recently_completed"]["$sum"]["$cond"]["if"]["$and"][1]
    assert completed_after == {"$gt": ["$completed", threshold]}
    assert set(stats.TASK_COUNT_STATUSES) <= set(group)


def test_contributions_pipeline_leaves_hidden_tasks_out_and_offsets_the_days():
    period_start = datetime(2026, 10, 27, 22, 0)

    pipeline = stats.contributions_pipeline("c", ["p1"], period_start, utc_offset_minutes=120)

    match = pipeline[0]["$match"]
    assert match["system_tags"] == {"$ne": EntityVisibility.hidden.value}
    assert match["created"] == {"$gte": period_start}
    day_group = pipeline[1]["$facet"]["days"][0]["$group"]
    assert day_group["_id"]["$dateToString"]["timezone"] == "+02:00"


# --- period and offset ----------------------------------------------------------------------------------------------


def test_contributions_period_start_follows_the_offset():
    # at +02:00 the period of 3 local days starts 2026-10-28 00:00 local, which is 2026-10-27 22:00 UTC
    assert stats.contributions_period_start(NOW, utc_offset_minutes=120, period_days=3) == datetime(2026, 10, 27, 22)


def test_contributions_period_start_at_utc_counts_today_and_the_days_before():
    assert stats.contributions_period_start(NOW, utc_offset_minutes=0, period_days=1) == datetime(2026, 10, 30)
    assert stats.contributions_period_start(NOW, utc_offset_minutes=0, period_days=7) == datetime(2026, 10, 24)


@pytest.mark.parametrize(
    "offset, suffix",
    [(-210, "-03:30"), (0, "+00:00"), (840, "+14:00"), (-600, "-10:00")],
)
def test_utc_offset_suffix(offset, suffix):
    assert stats.utc_offset_suffix(offset) == suffix


# --- shaping ----------------------------------------------------------------------------------------------------------


def test_aggregate_project_data_adds_the_subprojects_of_each_project():
    child_projects = {"p1": [child("c1")]}

    result = stats.aggregate_project_data(
        func=lambda a, b: a + b,
        project_ids=["p1", "p2"],
        child_projects=child_projects,
        data={"p1": 1, "c1": 10, "p2": 100},
    )

    assert result == {"p1": 11, "p2": 100}


def test_sum_runtime_takes_the_latest_task_start_and_adds_the_counters():
    early, late = datetime(2026, 10, 28), datetime(2026, 10, 29)

    result = stats.sum_runtime(
        {"total_runtime": 5, "completed": 1, "max_task_started": early},
        {"total_runtime": 7, "completed": 2, "max_task_started": late},
    )

    assert result == {"total_runtime": 12, "completed": 3, "max_task_started": late}


def test_active_stats_of_a_project_without_runtime_are_zero_and_have_no_last_run():
    result = stats.active_stats_by_project(["p1"], runtime={}, child_projects={})

    active = result["p1"]["active"]
    assert active["status_count"] == {status: 0 for status in stats.TASK_COUNT_STATUSES}
    assert active["total_tasks"] == 0
    assert active["last_task_run"] is None


def test_active_stats_sum_the_status_counts_of_the_subprojects():
    runtime = {
        "p1": {TaskStatus.completed: 1, "total_runtime": 3},
        "c1": {TaskStatus.completed: 2, TaskStatus.failed: 1, "total_runtime": 4},
    }

    result = stats.active_stats_by_project(["p1"], runtime=runtime, child_projects={"p1": [child("c1")]})

    active = result["p1"]["active"]
    assert active["status_count"][TaskStatus.completed] == 3
    assert active["status_count"][TaskStatus.failed] == 1
    assert active["total_tasks"] == 4
    assert active["total_runtime"] == 7


def test_dataset_stats_by_project_merges_tags_and_defaults_to_zero():
    rows = [
        {"_id": "p1", "count": 1, "tags": [["a"]]},
        {"_id": "c1", "count": 2, "tags": [["b", "a"]]},
    ]

    result = stats.dataset_stats_by_project(rows, ["p1", "p2"], child_projects={"p1": [child("c1")]})

    assert result == {
        "p1": {"datasets": {"count": 3, "tags": ["a", "b"]}},
        "p2": {"datasets": {"count": 0, "tags": []}},
    }


def test_defaults_are_not_shared_between_projects():
    result = stats.dataset_stats_by_project([], ["p1", "p2"], child_projects={})

    result["p1"]["datasets"]["tags"].append("x")
    assert result["p2"]["datasets"]["tags"] == []


def test_dataset_runtime_by_project_defaults_the_missing_counters():
    rows = [{"_id": "p1", "runtime": {"ds_file_count": 2}}]

    assert stats.dataset_runtime_by_project(rows) == {"p1": {"file_count": 2, "total_size": 0}}


def test_own_counts_default_to_zero_per_project():
    assert stats.own_count_by_project(["p1", "p2"], {"p1": 3}, name="own_datasets") == {
        "p1": {"own_datasets": 3},
        "p2": {"own_datasets": 0},
    }
    assert stats.own_contents_by_project(["p1"], tasks={}, models={"p1": 2}) == {
        "p1": {"own_tasks": 0, "own_models": 2}
    }


def test_contributor_ids_are_the_top_of_the_ranking_without_the_empty_user():
    result = {"users": [{"_id": "u1"}, {"_id": None}, {"_id": "u2"}, {"_id": "u3"}]}

    # the first `limit` entries of the ranking are taken, and the empty user is left out of them
    assert stats.contributor_ids(result, limit=2) == ["u1"]
    assert stats.contributor_ids(result, limit=10) == ["u1", "u2", "u3"]


def test_contributions_response_counts_statuses_and_running_runs():
    result = {
        "statuses": [
            {"_id": TaskStatus.completed, "runs": 2},
            {"_id": TaskStatus.in_progress, "runs": 1},
            {"_id": TaskStatus.queued, "runs": 1},
            {"_id": TaskStatus.failed, "runs": 3},
        ],
        "days": [{"_id": "2026-10-30", "runs": 7}],
        "users": [{"_id": "u1", "runs": 4}, {"_id": "u2", "runs": 3}],
    }

    response = stats.contributions_response(3, result, names={"u1": "Alice"}, limit=1)

    assert response == {
        "period_days": 3,
        "total": 7,
        "completed": 2,
        "failed": 3,
        "running": 2,
        "days": [{"date": "2026-10-30", "runs": 7}],
        "contributors": [{"id": "u1", "name": "Alice", "runs": 4}],
        "contributor_count": 2,
    }


def test_children_info_is_sorted_by_name():
    child_projects = {"p1": [child("b", "zeta"), child("a", "alpha")]}

    assert stats.children_info(["p1", "p2"], child_projects) == {
        "p1": [{"id": "a", "name": "alpha"}, {"id": "b", "name": "zeta"}],
        "p2": [],
    }


# --- selected children ----------------------------------------------------------------------------------------------


def test_selected_children_refuse_a_request_without_users_or_type_before_any_lookup():
    def lookup(_):
        raise AssertionError("no database lookup before the request is validated")

    with pytest.raises(errors.bad_request.ValidationError):
        stats.selected_children_queries("c", None, True, None, None, None, project_ids=["p1"], with_children=lookup)


def test_selected_children_of_datasets_query_the_dataset_projects():
    project_query, queries = stats.selected_children_queries(
        "c", ["u"], True, ProjectChildrenType.dataset, None, None, project_ids=None, with_children=None
    )

    assert project_query is None
    assert list(queries) == [Project]


def test_selected_children_of_reports_query_the_tasks():
    _, queries = stats.selected_children_queries(
        "c", ["u"], True, ProjectChildrenType.report, None, None, project_ids=None, with_children=None
    )

    assert list(queries) == [Task]


def test_selected_children_without_a_type_query_the_projects_and_their_children():
    project_query, queries = stats.selected_children_queries(
        "c", ["u"], True, None, None, None, project_ids=None, with_children=None
    )

    assert project_query is not None
    assert list(queries) == [Task, Model]


def test_selected_children_expand_the_project_ids_only_when_given():
    calls = []

    def expand(ids):
        calls.append(list(ids))
        return ["p1", "p1-sub"]

    stats.selected_children_queries("c", ["u"], True, None, None, None, project_ids=None, with_children=expand)
    assert calls == []

    stats.selected_children_queries("c", ["u"], True, None, None, None, project_ids=["p1"], with_children=expand)
    assert calls == [["p1"]]


def test_selected_ids_within_restricts_to_the_requested_projects():
    assert stats.selected_ids_within(["p1", "p2"], ["p2", "p3"]) == ["p2"]
    assert sorted(stats.selected_ids_within(["p1", "p2"], None)) == ["p1", "p2"]
