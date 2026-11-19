# apiserver/tests/services/test_project_stats_and_version_rules_characterisation_273.py
#
# Characterisation tests of the project statistics functions and of the version-dependent task field rules
# (tracking issue #267, step 4, issue #273). They pin what main does today, odd behaviour included, so that the
# refactor steps cannot change it silently. Production code is not changed here.
#
# A case marked "# suspect: pinned as is, see the decision issue (#282)" looks unintended. It is pinned as it is
# and listed in the pull request under "found, not changed".
#
# The statistics cases run against a real MongoDB (aggregations run in MongoDB, not in Elasticsearch). Only the
# cases that read the request pipeline (C29) stub Task.aggregate, and they say so in their docstring.
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.projects import ProjectChildrenType
from apiserver.bll.project.project_bll import ProjectBLL, dataset_tag, reports_tag
from apiserver.bll.task.compat import (
    DockerCmdBackwardsCompatibility,
    ModelsBackwardsCompatibility,
    legacy_params_prepare_for_save,
)
from apiserver.database.model import EntityVisibility
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.database.model.task.task import Task, TaskModelTypes, TaskStatus
from apiserver.database.model.user import UserProfile
from apiserver.database.utils import get_options
from apiserver.services import tasks as tasks_service
from apiserver.bll.compat import conform_tags, unescape_metadata
from apiserver.tests.db import ensure_db
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper
from apiserver.utilities.partial_version import PartialVersion

ensure_db()

NOW = datetime(2026, 10, 30, 12, 0)


@pytest.fixture
def company():
    # Every test gets its own company so that rows of other tests never interfere
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture
def ids():
    # Project, task and user ids are global in the database, so each test uses its own suffix
    suffix = uuid4().hex[:8]
    return SimpleNamespace(
        root=f"root-{suffix}",
        root2=f"root2-{suffix}",
        sub=f"sub-{suffix}",
        sub1=f"sub1-{suffix}",
        sub2=f"sub2-{suffix}",
        ds1=f"ds1-{suffix}",
        ds2=f"ds2-{suffix}",
        ua=f"ua-{suffix}",
        ub=f"ub-{suffix}",
        uc=f"uc-{suffix}",
    )


def make_project(
    company,
    id_,
    owner="owner1",
    parent=None,
    system_tags=None,
    tags=None,
    visibility=ProjectVisibility.public,
    name=None,
):
    project = Project(
        id=id_,
        company=company,
        user=owner,
        name=name or id_,
        basename=name or id_,
        created=datetime.utcnow(),
        visibility=visibility,
        parent=parent.id if parent else None,
        path=[*(parent.path or []), parent.id] if parent else [],
        system_tags=system_tags or [],
        tags=tags or [],
    )
    project.save()
    return project


def make_task(
    company,
    project_id,
    user="owner1",
    status=TaskStatus.created,
    id_=None,
    started=None,
    completed=None,
    created=None,
    system_tags=None,
    runtime=None,
    last_update=None,
    tags=None,
):
    task = Task(
        id=id_ or f"t-{uuid4().hex[:12]}",
        company=company,
        user=user,
        project=project_id,
        name="run",
        type="training",
        created=created or datetime.utcnow(),
        status=status,
        started=started,
        completed=completed,
        system_tags=system_tags or [],
        tags=tags or [],
        runtime=runtime or {},
        last_update=last_update,
    )
    task.save()
    return task


def make_model(company, project_id, user="owner1", id_=None):
    model = Model(
        id=id_ or f"m-{uuid4().hex[:12]}",
        company=company,
        user=user,
        project=project_id,
        name="model",
        created=datetime.utcnow(),
        ready=True,
    )
    model.save()
    return model


def make_user(company, id_, name):
    UserProfile(id=id_, company=company, name=name).save()


# --- statistics: project and dataset stats ------------------------------------------------------------------------


def test_dataset_stats_counts_direct_datasets_and_merges_tags(company, ids):
    # C1
    root = make_project(company, ids.root)
    make_project(company, ids.ds1, parent=root, system_tags=[dataset_tag], tags=["a"])
    make_project(company, ids.ds2, parent=root, system_tags=[dataset_tag], tags=["a", "b"])

    stats, children = ProjectBLL.get_project_dataset_stats(company, [ids.root], include_children=False)

    assert stats[ids.root] == {"datasets": {"count": 2, "tags": ["a", "b"]}}
    assert children == {ids.root: []}


def test_dataset_stats_with_children_adds_subproject_datasets(company, ids):
    # C2
    root = make_project(company, ids.root)
    sub = make_project(company, ids.sub, parent=root, name="sub")
    make_project(company, ids.ds1, parent=root, system_tags=[dataset_tag], tags=["a"])
    make_project(company, ids.ds2, parent=sub, system_tags=[dataset_tag], tags=["b"])

    stats, children = ProjectBLL.get_project_dataset_stats(company, [ids.root])

    assert stats[ids.root] == {"datasets": {"count": 2, "tags": ["a", "b"]}}
    # the children list holds every descendant project, the dataset projects included (sorted by name)
    assert children == {
        ids.root: [
            {"id": ids.ds1, "name": ids.ds1},
            {"id": ids.ds2, "name": ids.ds2},
            {"id": ids.sub, "name": "sub"},
        ]
    }


def test_dataset_stats_of_an_empty_project(company, ids):
    # C3
    make_project(company, ids.root)

    stats, children = ProjectBLL.get_project_dataset_stats(company, [ids.root])

    assert stats == {ids.root: {"datasets": {"count": 0, "tags": []}}}
    assert children == {ids.root: []}


def test_dataset_stats_respects_users(company, ids):
    # C4
    root = make_project(company, ids.root)
    make_project(company, ids.ds1, owner=ids.ua, parent=root, system_tags=[dataset_tag])
    make_project(company, ids.ds2, owner=ids.ub, parent=root, system_tags=[dataset_tag])

    stats, _ = ProjectBLL.get_project_dataset_stats(company, [ids.root], users=[ids.ua])

    assert stats[ids.root]["datasets"]["count"] == 1


def test_dataset_stats_restricted_to_selected_children(company, ids):
    # C5
    root = make_project(company, ids.root)
    sub1 = make_project(company, ids.sub1, parent=root)
    sub2 = make_project(company, ids.sub2, parent=root)
    make_project(company, ids.ds1, parent=sub1, system_tags=[dataset_tag])
    make_project(company, ids.ds2, parent=sub2, system_tags=[dataset_tag])
    make_project(company, f"x-{ids.ds1}", parent=sub2, system_tags=[dataset_tag])

    stats, _ = ProjectBLL.get_project_dataset_stats(company, [ids.root], selected_project_ids=[ids.sub1])

    assert stats[ids.root]["datasets"]["count"] == 1


def test_dataset_filter_is_mutated_in_place_and_seen_by_the_second_caller(company, ids):
    # C6 (the contract the refactor is to make: the caller's filter is not changed) and the probe of section 1.3.
    # suspect: pinned as is, see the decision issue (#282)
    make_project(company, ids.root)
    shared_filter = {"system_tags": ["x"]}

    ProjectBLL.get_project_dataset_stats(company, [ids.root], filter_=shared_filter)

    # the caller's dict and its list are changed: dataset is appended to the caller's list
    assert shared_filter == {"system_tags": ["x"]}


def test_second_caller_of_the_shared_filter_sees_the_appended_tag_and_gets_the_same_result(company, ids):
    # C6 part 2: the second caller of the shared filter (section 1.3 of #273) sees the tag appended by the first one
    # and builds the same $match as with a fresh copy.
    # suspect: pinned as is, see the decision issue (#282)
    root = make_project(company, ids.root)
    make_project(company, ids.ds1, parent=root, system_tags=[dataset_tag, "x"], tags=["a"])
    make_project(company, ids.ds2, parent=root, system_tags=[dataset_tag], tags=["a"])
    shared_filter = {"system_tags": ["x"]}

    own = ProjectBLL.calc_own_datasets(company, [ids.root], filter_=shared_filter)
    seen_by_second = dict(shared_filter)
    stats, _ = ProjectBLL.get_project_dataset_stats(company, [ids.root], include_children=False, filter_=shared_filter)
    fresh_stats, _ = ProjectBLL.get_project_dataset_stats(
        company,
        [ids.root],
        include_children=False,
        filter_={"system_tags": ["x"]},
    )

    assert seen_by_second == {"system_tags": ["x"]}
    # the legacy list filter matches any listed tag, so "x" OR "dataset" counts both datasets (main behaviour)
    assert own == {ids.root: {"own_datasets": 2}}
    assert stats == fresh_stats


def test_project_stats_counts_tasks_by_status(company, ids):
    # C7
    root = make_project(company, ids.root)
    t0 = datetime(2026, 10, 29, 10, 0, 0)
    make_task(
        company,
        root.id,
        status=TaskStatus.completed,
        started=t0,
        completed=t0 + timedelta(seconds=10),
    )
    make_task(company, root.id, status=TaskStatus.in_progress)
    make_task(company, root.id, status=TaskStatus.failed)

    stats, _ = ProjectBLL.get_project_stats(company, [ids.root])

    active = stats[ids.root]["active"]
    assert set(active["status_count"]) == set(get_options(TaskStatus))
    assert active["status_count"][TaskStatus.completed] == 1
    assert active["status_count"][TaskStatus.in_progress] == 1
    assert active["status_count"][TaskStatus.failed] == 1
    assert (
        sum(
            v
            for k, v in active["status_count"].items()
            if k not in (TaskStatus.completed, TaskStatus.in_progress, TaskStatus.failed)
        )
        == 0
    )
    assert active["total_tasks"] == 3
    assert active["total_runtime"] == 10


def test_project_stats_recently_completed_is_24h(company, ids):
    # C8
    root = make_project(company, ids.root)
    now = datetime.utcnow()
    make_task(
        company,
        root.id,
        status=TaskStatus.completed,
        started=now - timedelta(hours=2),
        completed=now - timedelta(hours=1),
    )
    make_task(
        company,
        root.id,
        status=TaskStatus.completed,
        started=now - timedelta(days=3),
        completed=now - timedelta(days=2),
    )

    stats, _ = ProjectBLL.get_project_stats(company, [ids.root])

    assert stats[ids.root]["active"]["completed_tasks_24h"] == 1


def test_project_stats_last_task_run(company, ids):
    # C9
    root = make_project(company, ids.root)
    empty = make_project(company, ids.sub, parent=None)
    t1 = datetime(2026, 10, 28, 9, 0, 0)
    t2 = datetime(2026, 10, 29, 9, 0, 0)
    make_task(company, root.id, started=t1, completed=t1 + timedelta(seconds=5), status=TaskStatus.completed)
    make_task(company, root.id, started=t2, completed=t2 + timedelta(seconds=5), status=TaskStatus.completed)

    stats, _ = ProjectBLL.get_project_stats(company, [ids.root, empty.id])

    assert stats[ids.root]["active"]["last_task_run"] == t2
    assert stats[empty.id]["active"]["last_task_run"] is None


def test_project_stats_excludes_archived_tasks(company, ids):
    # C10
    root = make_project(company, ids.root)
    make_task(company, root.id, system_tags=[EntityVisibility.archived.value])
    make_task(company, root.id)

    stats, _ = ProjectBLL.get_project_stats(company, [ids.root])

    assert stats[ids.root]["active"]["total_tasks"] == 1


def test_project_stats_hidden_tasks_are_counted(company, ids):
    # C11: the issue asks to confirm this with the owner; pinned as main counts them.
    # suspect: pinned as is, see the decision issue (#282)
    root = make_project(company, ids.root)
    make_task(company, root.id, system_tags=[EntityVisibility.hidden.value])

    stats, _ = ProjectBLL.get_project_stats(company, [ids.root])

    assert stats[ids.root]["active"]["total_tasks"] == 1


def test_project_stats_sums_subprojects_only_with_children(company, ids):
    # C12
    root = make_project(company, ids.root)
    sub = make_project(company, ids.sub, parent=root)
    make_task(company, root.id)
    make_task(company, sub.id)

    with_children, _ = ProjectBLL.get_project_stats(company, [ids.root], include_children=True)
    without_children, _ = ProjectBLL.get_project_stats(company, [ids.root], include_children=False)

    assert with_children[ids.root]["active"]["total_tasks"] == 2
    assert without_children[ids.root]["active"]["total_tasks"] == 1


def test_project_stats_hidden_subprojects_follow_search_hidden(company, ids):
    # C13
    root = make_project(company, ids.root)
    sub = make_project(company, ids.sub, parent=root, system_tags=[EntityVisibility.hidden.value])
    make_task(company, sub.id)

    hidden_out, _ = ProjectBLL.get_project_stats(company, [ids.root], search_hidden=False)
    hidden_in, _ = ProjectBLL.get_project_stats(company, [ids.root], search_hidden=True)

    assert hidden_out[ids.root]["active"]["total_tasks"] == 0
    assert hidden_in[ids.root]["active"]["total_tasks"] == 1


def test_dataset_runtime_uses_the_latest_runtime(company, ids):
    # C14
    root = make_project(company, ids.root)
    now = datetime.utcnow()
    make_task(
        company,
        root.id,
        runtime={"ds_file_count": 2, "ds_total_size": 1000},
        last_update=now - timedelta(hours=2),
    )
    make_task(
        company,
        root.id,
        runtime={"ds_file_count": 5, "ds_total_size": 7},
        last_update=now - timedelta(hours=1),
    )

    stats = ProjectBLL.get_dataset_stats(company, [ids.root])

    assert stats == {ids.root: {"file_count": 5, "total_size": 7}}


def test_dataset_runtime_absent_without_runtime(company, ids):
    # C15
    root = make_project(company, ids.root)
    make_task(company, root.id)

    stats = ProjectBLL.get_dataset_stats(company, [ids.root])

    assert ids.root not in stats


def test_own_datasets_counts_direct_children_only(company, ids):
    # C16
    root = make_project(company, ids.root)
    sub = make_project(company, ids.sub, parent=root)
    make_project(company, ids.ds1, parent=root, system_tags=[dataset_tag])
    make_project(company, ids.ds2, parent=sub, system_tags=[dataset_tag])
    make_project(company, f"p-{ids.ds1}", parent=root)  # a subproject without the dataset tag

    stats = ProjectBLL.calc_own_datasets(company, [ids.root, ids.sub])

    assert stats == {ids.root: {"own_datasets": 1}, ids.sub: {"own_datasets": 1}}


def test_own_datasets_respects_a_filter_without_mutating_it(company, ids):
    # C17: the caller's dict is changed as well (section 1.3)
    # suspect: pinned as is, see the decision issue (#282)
    root = make_project(company, ids.root)
    sub = make_project(company, ids.sub, parent=root)
    make_project(company, ids.ds1, parent=root, system_tags=[dataset_tag], tags=["t"])
    make_project(company, ids.ds2, parent=sub, system_tags=[dataset_tag], tags=[])
    filter_ = {"tags": ["t"]}

    stats = ProjectBLL.calc_own_datasets(company, [ids.root, ids.sub], filter_=filter_)

    assert stats == {ids.root: {"own_datasets": 1}, ids.sub: {"own_datasets": 0}}
    assert filter_ == {"tags": ["t"]}


def test_own_contents_counts_tasks_and_models(company, ids):
    # C18
    root = make_project(company, ids.root)
    make_task(company, root.id)
    make_task(company, root.id)
    make_task(company, root.id, system_tags=[EntityVisibility.archived.value])
    make_model(company, root.id)

    stats = ProjectBLL.calc_own_contents(company, [ids.root])

    assert stats == {ids.root: {"own_tasks": 2, "own_models": 1}}


def test_own_contents_of_an_empty_project(company, ids):
    # C19
    make_project(company, ids.root)

    stats = ProjectBLL.calc_own_contents(company, [ids.root])

    assert stats == {ids.root: {"own_tasks": 0, "own_models": 0}}


def test_own_contents_explicit_archived_filter_keeps_archived(company, ids):
    # C20: no -archived is added when the caller names archived
    root = make_project(company, ids.root)
    make_task(company, root.id, system_tags=[EntityVisibility.archived.value])
    make_task(company, root.id)

    stats = ProjectBLL.calc_own_contents(
        company,
        [ids.root],
        filter_={"system_tags": [EntityVisibility.archived.value]},
    )

    assert stats[ids.root]["own_tasks"] == 1


def test_selected_children_requires_users_or_children_type(company):
    # C21
    with pytest.raises(errors.bad_request.ValidationError):
        ProjectBLL.get_projects_with_selected_children(company, users=None, children_type=None)


def test_selected_children_of_a_dataset_child(company, ids):
    # C22
    root = make_project(company, ids.root)
    make_project(company, ids.ds1, owner=ids.ua, parent=root, system_tags=[dataset_tag])

    filtered, selected = ProjectBLL.get_projects_with_selected_children(
        company,
        users=[ids.ua],
        children_type=ProjectChildrenType.dataset,
    )

    assert ids.root in filtered
    assert ids.root in selected


def test_selected_children_of_a_report(company, ids):
    # C23
    root = make_project(company, ids.root)
    make_task(company, root.id, user=ids.ua, system_tags=[reports_tag])

    filtered, selected = ProjectBLL.get_projects_with_selected_children(
        company,
        users=[ids.ua],
        children_type="report",
    )

    assert ids.root in filtered
    assert ids.root in selected


def test_selected_children_intersects_project_ids(company, ids):
    # C24
    root = make_project(company, ids.root)
    root2 = make_project(company, ids.root2)
    make_project(company, ids.ds1, owner=ids.ua, parent=root, system_tags=[dataset_tag])
    make_project(company, ids.ds2, owner=ids.ua, parent=root2, system_tags=[dataset_tag])

    filtered, _ = ProjectBLL.get_projects_with_selected_children(
        company,
        users=[ids.ua],
        project_ids=[ids.root],
        children_type="dataset",
    )

    assert filtered == [ids.root]


def test_contributors_limit_keeps_the_total(company, ids):
    # C25
    make_project(company, ids.root)
    for user_id, name in ((ids.ua, "Alice"), (ids.ub, "Bob"), (ids.uc, "Carol")):
        make_user(company, user_id, name)
    make_task(company, ids.root, user=ids.ua, created=NOW - timedelta(hours=1))
    make_task(company, ids.root, user=ids.ub, created=NOW - timedelta(hours=2))
    make_task(company, ids.root, user=ids.uc, created=NOW - timedelta(hours=3))

    result = ProjectBLL.get_contributions(company, ids.root, period_days=3, utc_offset_minutes=0, limit=1, now=NOW)

    assert len(result["contributors"]) == 1
    assert result["contributor_count"] == 3


def test_contributions_period_start_follows_offset(company, ids):
    # C28: at +02:00 the period of 3 local days starts 2026-10-28 00:00 local = 2026-10-27 22:00 UTC.
    # A run at 22:30 UTC is inside (local 28th), a run at 21:30 UTC is outside (local 27th).
    make_project(company, ids.root)
    make_user(company, ids.ua, "Zoe")
    make_task(company, ids.root, user=ids.ua, created=datetime(2026, 10, 27, 22, 30))
    make_task(company, ids.root, user=ids.ua, created=datetime(2026, 10, 27, 21, 30))

    result = ProjectBLL.get_contributions(company, ids.root, period_days=3, utc_offset_minutes=120, limit=5, now=NOW)

    assert result["total"] == 1
    assert result["days"] == [{"date": "2026-10-28", "runs": 1}]


@pytest.mark.parametrize(
    "offset, suffix",
    [(-210, "-03:30"), (0, "+00:00"), (840, "+14:00")],
)
def test_utc_offset_suffix(monkeypatch, company, ids, offset, suffix):
    # C29: stubbed. The suffix is built inside get_contributions and only reaches the request pipeline, so Task.aggregate
    # is replaced by a recorder that returns no rows. No database rows are read.
    captured = {}

    def record(pipeline):
        captured["pipeline"] = pipeline
        return iter([])

    monkeypatch.setattr(Task, "aggregate", record)

    ProjectBLL.get_contributions(company, ids.root, period_days=3, utc_offset_minutes=offset, limit=5, now=NOW)

    facet = captured["pipeline"][1]["$facet"]
    timezone = facet["days"][0]["$group"]["_id"]["$dateToString"]["timezone"]
    assert timezone == suffix


# --- version-dependent task field rules ----------------------------------------------------------------------------
#
# The helpers read only the requested version (and, where it is kept for the record, the actual one). A stub call
# is enough: no database is needed for these cases.


def _call(requested: str, actual: str | None = None, endpoint: str = "tasks.create"):
    return SimpleNamespace(
        endpoint_name=endpoint,
        requested_endpoint_version=PartialVersion(requested),
        actual_endpoint_version=PartialVersion(actual or requested),
    )


class _PreviousTask:
    def __init__(self, hyperparams):
        self.hyperparams = hyperparams

    def to_proper_dict(self):
        return {"hyperparams": self.hyperparams}


def _legacy(name, value, section="Args"):
    return {"name": name, "type": "legacy", "value": value, "section": section}


def test_legacy_write_at_2_8_converts_the_parameters(company):
    # U1: the boundary below 2.9 on the write path (the read side is pinned at 2.8 already)
    fields = {"execution": {"parameters": {"a": 1}}}

    tasks_service.prepare_for_save(_call("2.8"), fields)

    assert fields["hyperparams"] == {"Args": {"a": _legacy("a", "1")}}
    assert "parameters" not in fields["execution"]


def test_execution_model_desc_has_no_write_path_below_2_9_but_is_copied_back_on_read():
    # U2: a pre-2.9 client's model_desc is not converted on write, while the configuration is copied back on read.
    # suspect: pinned as is, see the decision issue (#282)
    fields = {"execution": {"model_desc": {"x": "1"}}}

    tasks_service.prepare_for_save(_call("2.1"), fields)

    # the write leaves model_desc where the client put it, with no hyperparams or configuration created
    assert fields == {"execution": {"model_desc": {"x": "1"}}}

    data = {"configuration": {"cfg": {"name": "cfg", "type": "legacy", "value": "x", "section": "Args"}}}
    tasks_service.conform_task_data(_call("2.1"), data)

    assert data["execution"]["model_desc"] == {"cfg": "x"}


def test_tf_define_params_are_copied_back_on_read_below_2_9():
    # U3: the read copy of TF_DEFINE params is keyed TF_DEFINE/<name>
    data = {
        "hyperparams": {
            "Args": {"a": _legacy("a", "1")},
            "TF_DEFINE": {"b": _legacy("b", "x", section="TF_DEFINE")},
        }
    }

    tasks_service.conform_task_data(_call("2.1"), data)

    assert data["execution"]["parameters"] == {"a": "1", "TF_DEFINE/b": "x"}


def test_the_2_9_threshold_through_conform_task_data_with_a_list_input():
    # U4: the list branch (the existing test uses a dict)
    def make():
        return {"hyperparams": {"Args": {"a": _legacy("a", "1")}}}

    below = [make(), make()]
    tasks_service.conform_task_data(_call("2.8"), below)
    at = [make(), make()]
    tasks_service.conform_task_data(_call("2.9"), at)

    assert all(d["execution"]["parameters"] == {"a": "1"} for d in below)
    assert all("execution" not in d for d in at)


def test_empty_model_value_on_write_gives_an_empty_list_or_no_key_below_2_13():
    # U5: an empty string gives an empty models list, None leaves the field and creates no models key
    empty = {"execution": {"model": ""}}
    ModelsBackwardsCompatibility.prepare_for_save(_call("2.12"), empty)

    assert empty["models"] == {TaskModelTypes.input: []}
    assert "model" not in empty["execution"]

    none = {"execution": {"model": None}}
    ModelsBackwardsCompatibility.prepare_for_save(_call("2.12"), none)

    assert "models" not in none
    assert none == {"execution": {"model": None}}


def test_docker_command_without_arguments_on_write_and_on_read():
    # U6
    fields = {"execution": {"docker_cmd": "img"}}
    DockerCmdBackwardsCompatibility.prepare_for_save(_call("2.12"), fields)

    assert fields["container"] == {"image": "img", "arguments": ""}

    data = {"container": {"image": "img", "arguments": ""}}
    DockerCmdBackwardsCompatibility.unprepare_from_saved(_call("2.12"), data)

    assert data["execution"]["docker_cmd"] == "img"


def test_several_models_on_read_take_the_last_output_and_the_first_input():
    # U7
    data = {
        "models": {
            TaskModelTypes.input: [{"model": "in1"}, {"model": "in2"}],
            TaskModelTypes.output: [{"model": "out1"}, {"model": "out2"}],
        }
    }

    ModelsBackwardsCompatibility.unprepare_from_saved(_call("2.12"), data)

    assert data["execution"]["model"] == "in1"
    assert data["output"]["model"] == "out2"


def test_legacy_params_without_request_params_and_previous_params_are_left_alone():
    # U8: the early return: no legacy params in the request and none in the previous task
    fields = {"execution": {"parameters": {}}}
    keep = {"name": "keep", "type": "float", "value": "2", "section": "Args"}
    previous = _PreviousTask({"Args": {"keep": keep}})

    legacy_params_prepare_for_save(fields, previous_task=previous)

    assert "hyperparams" not in fields
    assert fields == {"execution": {"parameters": {}}}


def test_empty_tf_define_name_on_write_is_refused():
    # U9
    fields = {"execution": {"parameters": {"TF_DEFINE/": 1}}}

    with pytest.raises(errors.bad_request.ValidationError):
        tasks_service.prepare_for_save(_call("2.1"), fields)


def test_legacy_conversion_runs_before_escaping_of_the_names():
    # U10: the converted names are escaped, so the conversion must come first
    fields = {"execution": {"parameters": {"a.b": 1}}}

    tasks_service.prepare_for_save(_call("2.1"), fields)

    escaped = ParameterKeyEscaper.escape("a.b")
    assert escaped != "a.b"
    assert list(fields["hyperparams"]["Args"]) == [escaped]


def test_models_and_docker_are_moved_by_prepare_for_save_below_2_13():
    # U11: the composition through the endpoint function, requested 2.12, actual 2.41 (the requested version decides)
    fields = {"execution": {"model": "m", "docker_cmd": "img --x"}}

    tasks_service.prepare_for_save(_call("2.12", actual="2.41"), fields)

    assert fields["models"][TaskModelTypes.input][0]["model"] == "m"
    assert fields["container"] == {"image": "img", "arguments": "--x"}
    assert "model" not in fields["execution"]
    assert "docker_cmd" not in fields["execution"]


def test_models_and_docker_are_kept_from_2_13_through_prepare_for_save():
    # U11 (the other side of the boundary)
    fields = {"execution": {"model": "m", "docker_cmd": "img --x"}}

    tasks_service.prepare_for_save(_call("2.13"), fields)

    assert fields["execution"] == {"model": "m", "docker_cmd": "img --x"}
    assert "models" not in fields
    assert "container" not in fields


def test_requested_above_the_server_level_uses_the_requested_value():
    # U12: a request from 2.42 (above the server level) is judged by the requested value: no legacy conversion
    fields = {"execution": {"parameters": {"a": 1}}}

    tasks_service.prepare_for_save(_call("2.42"), fields)

    assert "hyperparams" not in fields
    assert fields["execution"]["parameters"] == {"a": 1}


def test_metadata_boundary_at_2_16_and_2_17_on_a_non_get_by_id_endpoint():
    # U13: the metadata of a document is blanked at 2.16 and unescaped from 2.17, on any endpoint
    doc = {"metadata": {"a%2Eb": {"key": "k", "value": "v"}}}
    unescape_metadata(_call("2.16", endpoint="tasks.get_all"), doc)
    assert doc["metadata"] == []

    doc = {"metadata": {"a%2Eb": {"key": "k", "value": "v"}}}
    unescape_metadata(_call("2.17", endpoint="tasks.get_all"), doc)
    assert doc["metadata"] == {ParameterKeyEscaper.unescape("a%2Eb"): {"key": "k", "value": "v"}}


def test_tag_split_at_requested_2_2_keeps_tags_when_system_tags_are_set():
    # U14: _upgrade_tags returns the input unchanged when system_tags is already set
    tags, system_tags = conform_tags(_call("2.2"), ["a"], ["s"])

    assert tags == ["a"]
    assert system_tags == ["s"]
