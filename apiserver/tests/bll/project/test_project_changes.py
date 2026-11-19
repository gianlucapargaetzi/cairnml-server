"""Test X3 (issue 276): the project-change plan returns exactly the bookkeeping pinned by the call-site table of
issue 276 (section 2.2) for tasks.create, tasks.update and tasks.edit.

The plan is pure: it decides which tag-cache resets, tag-cache invalidations, project-time updates and repository
recordings a write needs. The characterisation tests (issues 268, 270) pin the same sets against the real service;
these cases pin the plan itself, without a database.
"""

from apiserver.bll.project.project_changes import ProjectEffects, plan_task_batch, plan_task_create, plan_task_write

FIELDS = {"tags": ["fresh"], "system_tags": None}


def test_x3_create_invalidates_the_tag_cache_of_its_project_and_bumps_its_time_and_records_the_repository():
    effects = plan_task_create("root1", {"tags": None, "system_tags": None})

    assert effects == ProjectEffects(
        reset_projects=None,
        update_tags=("root1", {"tags": None, "system_tags": None}),
        project_time=("root1",),
        record_repository=True,
    )


def test_x3_update_of_a_name_only_touches_the_tag_cache_and_calls_project_time_with_none():
    # Table: update -> T(old, updated_fields); P(updated_fields.get("project")) is P(None), a no-op call
    effects = plan_task_write("root1", {"name": "renamed"}, updated=1)

    assert effects == ProjectEffects(
        reset_projects=None,
        update_tags=("root1", {"name": "renamed"}),
        project_time=(None,),
        record_repository=True,
    )


def test_x3_update_that_moves_a_task_resets_both_projects_and_bumps_only_the_new_one():
    effects = plan_task_write("src", {"project": "dst"}, updated=1)

    assert effects == ProjectEffects(
        reset_projects=("dst", "src"),
        update_tags=None,
        project_time=("dst",),
        record_repository=True,
    )


def test_x3_update_that_names_its_own_project_is_not_a_move():
    effects = plan_task_write("root1", {"project": "root1", "tags": ["x"]}, updated=1)

    assert effects.reset_projects is None
    assert effects.update_tags == ("root1", {"project": "root1", "tags": ["x"]})
    assert effects.project_time == ("root1",)


def test_x3_edit_on_the_same_project_invalidates_only_its_own_project():
    effects = plan_task_write("root1", dict(FIELDS), updated=1)

    assert effects == ProjectEffects(
        reset_projects=None,
        update_tags=("root1", FIELDS),
        project_time=(None,),
        record_repository=True,
    )


def test_x3_edit_that_moves_a_task_resets_both_projects_but_bumps_only_the_new_one():
    effects = plan_task_write("src", {"project": "dst", "last_change": "now"}, updated=1)

    assert effects.reset_projects == ("dst", "src")
    assert effects.update_tags is None
    assert effects.project_time == ("dst",)
    assert effects.record_repository is True


def test_x3_a_write_that_changed_nothing_has_no_effects():
    assert plan_task_write("src", {"project": "dst"}, updated=0) == ProjectEffects()


def test_x3_batch_resets_every_touched_project_and_sets_their_time_in_one_call_and_records_no_repository():
    # tasks.update_batch (issue 277, B7-B13): the same projects reset and bumped together, the repository is not recorded
    effects = plan_task_batch(["dst", "src"])

    assert effects == ProjectEffects(
        reset_projects=("dst", "src"),
        update_tags=None,
        project_time=(),
        project_time_together=("dst", "src"),
        record_repository=False,
    )


def test_x3_batch_with_no_projects_has_nothing_to_reset():
    assert plan_task_batch([]) == ProjectEffects(reset_projects=(), project_time_together=())
