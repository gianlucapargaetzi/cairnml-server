from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.apimodels.base import MoveRequest, MakePublicRequest
from apiserver.apimodels.models import (
    CreateModelRequest,
    DeleteModelRequest,
    GetFrameworksRequest,
    ModelRequest,
    ModelsDeleteManyRequest,
    ModelsGetRequest,
    UpdateModelRequest,
)
from apiserver.apimodels.batch import BatchRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.model import Model
from apiserver.database.model.project import (
    Project,
    ProjectMember,
    ProjectRole,
    ProjectVisibility,
)
from apiserver.service_repo.auth import Identity
from apiserver.services import models as svc
from apiserver.utilities.partial_version import PartialVersion
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    # Every test gets its own company so that rows from other tests never interfere
    return f"c-{uuid4().hex[:8]}"


def make_root(company, id_="root1", owner="owner1", visibility=ProjectVisibility.private, members=()):
    project = Project(
        id=id_,
        company=company,
        user=owner,
        name=f"Project-{id_}",
        basename=f"Project-{id_}",
        created=datetime.utcnow(),
        visibility=visibility,
        members=[
            ProjectMember(user=u, role=r, added=datetime.utcnow(), added_by=owner)
            for u, r in members
        ],
    )
    project.save()
    return project


def make_model(company, project_id, owner, id_="model1", framework=None):
    model = Model(
        id=id_,
        company=company,
        user=owner,
        project=project_id,
        name=f"Model-{id_}",
        created=datetime.utcnow(),
        framework=framework,
        ready=True,
    )
    model.save()
    return model


def identity(company, user, role=Role.user):
    return Identity(user=user, company=company, role=role)


class FakeCall:
    def __init__(self, ident, data=None):
        self.identity = ident
        self.data = data if data is not None else {}
        self.requested_endpoint_version = PartialVersion("2.30")
        self.result = SimpleNamespace(data=None, data_model=None)


def test_read_write_cannot_delete_someone_elses_model(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_model(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete(
            FakeCall(identity(company, "u2")),
            company,
            DeleteModelRequest(model="model1", force=True),
        )
    assert Model.objects(id="model1").first() is not None


@pytest.mark.needs_redis
def test_read_write_owner_can_delete_their_own_model(company, monkeypatch):
    monkeypatch.setattr(svc, "_delete_model_events", lambda **kwargs: None)
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_model(company, "root1", "u2")
    svc.delete(
        FakeCall(identity(company, "u2")),
        company,
        DeleteModelRequest(model="model1", force=True),
    )
    assert Model.objects(id="model1").first() is None


def test_collaborator_can_edit_someone_elses_model(company):
    make_root(company, members=[("u2", ProjectRole.collaborator)])
    make_model(company, "root1", "owner1")
    svc.edit(
        FakeCall(identity(company, "u2"), data={"name": "Renamed"}),
        company,
        UpdateModelRequest(model="model1"),
    )
    assert Model.objects(id="model1").first().name == "Renamed"


def test_read_write_cannot_edit_someone_elses_model(company):
    make_root(company, members=[("u2", ProjectRole.read_write)])
    make_model(company, "root1", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.edit(
            FakeCall(identity(company, "u2"), data={"name": "Renamed"}),
            company,
            UpdateModelRequest(model="model1"),
        )
    assert Model.objects(id="model1").first().name == "Model-model1"


def test_get_by_id_hides_a_model_in_a_private_project_from_a_stranger(company):
    make_root(company)
    make_model(company, "root1", "owner1")
    with pytest.raises(errors.bad_request.InvalidModelId):
        svc.get_by_id(
            FakeCall(identity(company, "stranger")), company, ModelRequest(model="model1")
        )


def test_get_all_lists_only_models_in_visible_projects(company):
    make_root(company, id_="privroot")
    make_root(company, id_="pubroot", visibility=ProjectVisibility.public)
    make_model(company, "privroot", "owner1", id_="hidden-model")
    make_model(company, "pubroot", "owner1", id_="shown-model")
    call = FakeCall(identity(company, "stranger"))
    svc.get_all(call, company, None)
    ids = {m["id"] for m in call.result.data["models"]}
    assert "shown-model" in ids
    assert "hidden-model" not in ids


def test_get_all_ex_hides_models_in_projects_the_caller_cannot_see(company):
    make_root(company, id_="privroot")
    make_model(company, "privroot", "owner1", id_="hidden-model")
    call = FakeCall(identity(company, "stranger"))
    svc.get_all_ex(call, company, ModelsGetRequest())
    assert "hidden-model" not in {m["id"] for m in call.result.data["models"]}


def test_a_model_without_a_project_is_not_blocked_by_the_project_checks(company):
    make_model(company, None, "owner1", id_="loose-model")
    call = FakeCall(identity(company, "stranger"))
    svc.get_by_id(call, company, ModelRequest(model="loose-model"))
    assert call.result.data["model"]["id"] == "loose-model"


def test_get_frameworks_without_projects_only_counts_visible_projects(company):
    make_root(company, id_="privroot")
    make_root(company, id_="pubroot", visibility=ProjectVisibility.public)
    make_model(company, "privroot", "owner1", id_="m-priv", framework="PrivateFramework")
    make_model(company, "pubroot", "owner1", id_="m-pub", framework="PublicFramework")
    call = FakeCall(identity(company, "stranger"))
    svc.get_frameworks(call, company, GetFrameworksRequest())
    assert call.result.data["frameworks"] == ["PublicFramework"]


def test_get_frameworks_for_a_caller_with_nothing_visible_returns_nothing(company):
    make_root(company, id_="privroot")
    make_model(company, "privroot", "owner1", id_="m-priv", framework="PrivateFramework")
    call = FakeCall(identity(company, "stranger"))
    svc.get_frameworks(call, company, GetFrameworksRequest())
    assert call.result.data["frameworks"] == []


def test_get_frameworks_refuses_a_named_private_project_to_a_stranger(company):
    make_root(company, id_="privroot")
    make_model(company, "privroot", "owner1", id_="m-priv", framework="PrivateFramework")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.get_frameworks(
            FakeCall(identity(company, "stranger")),
            company,
            GetFrameworksRequest(projects=["privroot"]),
        )


def test_move_needs_admin_on_the_destination_project(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    make_model(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["model1"], project="dst"),
        )
    assert Model.objects(id="model1").first().project == "src"


def test_move_needs_admin_on_the_current_project(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.collaborator)])
    make_model(company, "src", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.move(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            MoveRequest(ids=["model1"], project="dst"),
        )
    assert Model.objects(id="model1").first().project == "src"


def test_move_with_admin_on_both_projects_moves_the_model(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.collaborator)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.collaborator)])
    make_model(company, "src", "owner1")
    svc.move(
        FakeCall(identity(company, "u2"), data={"project": "dst"}),
        company,
        MoveRequest(ids=["model1"], project="dst"),
    )
    assert Model.objects(id="model1").first().project == "dst"


def test_create_needs_write_on_the_project(company):
    make_root(company, members=[("u2", ProjectRole.read)])
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.create(
            FakeCall(identity(company, "u2")),
            company,
            CreateModelRequest(name="new-model", uri="s3://x", project="root1"),
        )
    assert Model.objects(name="new-model", company=company).first() is None


def test_delete_many_is_all_or_nothing(company, monkeypatch):
    monkeypatch.setattr(svc, "_delete_model_events", lambda **kwargs: None)
    make_root(company, id_="pub", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="other", members=[("u2", ProjectRole.read)])
    make_model(company, "pub", "u2", id_="own-model")
    make_model(company, "other", "owner1", id_="foreign-model")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.delete_many(
            FakeCall(identity(company, "u2")),
            company,
            ModelsDeleteManyRequest(ids=["own-model", "foreign-model"], force=True),
        )
    assert Model.objects(id="own-model").first() is not None
    assert Model.objects(id="foreign-model").first() is not None


def test_archive_many_is_all_or_nothing(company):
    make_root(company, id_="pub", members=[("u2", ProjectRole.read_write)])
    make_root(company, id_="other", members=[("u2", ProjectRole.read)])
    make_model(company, "pub", "u2", id_="own-model")
    make_model(company, "other", "owner1", id_="foreign-model")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.archive_many(
            FakeCall(identity(company, "u2")),
            company,
            BatchRequest(ids=["own-model", "foreign-model"]),
        )
    assert "archived" not in (Model.objects(id="own-model").first().system_tags or [])


def test_edit_cannot_move_a_model_into_a_project_the_caller_cannot_admin(company):
    make_root(company, id_="src", members=[("u2", ProjectRole.collaborator)])
    make_root(company, id_="dst", members=[("u2", ProjectRole.read_write)])
    make_model(company, "src", "owner1")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.edit(
            FakeCall(identity(company, "u2"), data={"project": "dst"}),
            company,
            UpdateModelRequest(model="model1"),
        )
    assert Model.objects(id="model1").first().project == "src"


def test_clearing_the_project_through_edit_is_refused_before_any_change(company):
    # edit parses the project as a Project id; an empty value finds no project, which the parser
    # reports the same way it reports a hidden one: InvalidProjectId
    make_root(company, id_="root1", members=[("u2", ProjectRole.read_write)])
    make_model(company, "root1", "u2")
    with pytest.raises(errors.bad_request.InvalidProjectId):
        svc.edit(
            FakeCall(identity(company, "u2"), data={"project": ""}),
            company,
            UpdateModelRequest(model="model1"),
        )
    assert Model.objects(id="model1").first().project == "root1"


def test_clearing_the_project_helper_needs_admin_on_the_current_project(company):
    # Direct check of the destination helper: a read_write owner has write but not admin
    from apiserver.services.models import _assert_destination_project

    make_root(company, id_="root1", members=[("u2", ProjectRole.read_write)])
    model = make_model(company, "root1", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        _assert_destination_project(identity(company, "u2"), model, {"project": ""})
    # no project key, or the same project: nothing to check
    _assert_destination_project(identity(company, "u2"), model, {})
    _assert_destination_project(identity(company, "u2"), model, {"project": "root1"})


def test_read_write_owner_cannot_clear_the_project_through_update(company):
    make_root(company, id_="root1", members=[("u2", ProjectRole.read_write)])
    make_model(company, "root1", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(
            FakeCall(identity(company, "u2"), data={"project": ""}),
            company,
            UpdateModelRequest(model="model1"),
        )
    assert Model.objects(id="model1").first().project == "root1"


def test_read_write_owner_cannot_clear_the_project_with_null_through_update(company):
    # update does not parse the call data, so an explicit null reaches the check as well
    make_root(company, id_="root1", members=[("u2", ProjectRole.read_write)])
    make_model(company, "root1", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.update(
            FakeCall(identity(company, "u2"), data={"project": None}),
            company,
            UpdateModelRequest(model="model1"),
        )
    assert Model.objects(id="model1").first().project == "root1"


def test_admin_can_list_models_in_private_projects(company):
    make_root(company, id_="privroot")
    make_model(company, "privroot", "owner1", id_="hidden-model")
    call = FakeCall(identity(company, "admin-user", role=Role.admin))
    svc.get_all(call, company, None)
    assert "hidden-model" in {m["id"] for m in call.result.data["models"]}


def test_admin_gets_frameworks_of_private_projects_without_a_filter(company):
    make_root(company, id_="privroot")
    make_model(company, "privroot", "owner1", id_="m-priv", framework="PrivateFramework")
    call = FakeCall(identity(company, "admin-user", role=Role.admin))
    svc.get_frameworks(call, company, GetFrameworksRequest())
    assert "PrivateFramework" in call.result.data["frameworks"]


def test_admin_can_move_a_model_into_a_private_project(company):
    make_root(company, id_="src")
    make_root(company, id_="dst")
    make_model(company, "src", "owner1")
    svc.move(
        FakeCall(identity(company, "admin-user", role=Role.admin), data={"project": "dst"}),
        company,
        MoveRequest(ids=["model1"], project="dst"),
    )
    assert Model.objects(id="model1").first().project == "dst"


def test_make_public_refuses_a_read_write_member(company):
    make_root(company, id_="root1", members=[("u2", ProjectRole.read_write)])
    make_model(company, "root1", "u2")
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.make_public(FakeCall(identity(company, "u2")), company, MakePublicRequest(ids=["model1"]))
    assert Model.objects(id="model1").first().company == company


@pytest.mark.needs_redis
def test_admin_can_clear_the_project_of_a_model(company):
    # update passes the project through unparsed, so this is the path that can clear it
    make_root(company, id_="root1")
    make_model(company, "root1", "owner1")
    svc.update(
        FakeCall(identity(company, "admin-user", role=Role.admin), data={"project": ""}),
        company,
        UpdateModelRequest(model="model1"),
    )
    assert not Model.objects(id="model1").first().project


def test_get_by_task_id_refuses_a_task_in_a_private_project_to_a_stranger(company):
    # the deprecated lookup-by-task-id path must not look any different for a hidden task than
    # for a genuinely missing one
    from apiserver.database.model.task.task import Task, Models, ModelItem
    from apiserver.apimodels.models import TaskRequest as ModelsTaskRequest

    make_root(company, id_="root1")
    make_model(company, "root1", "owner1", id_="out-model")
    task = Task(
        id="task1", company=company, user="owner1", name="task1", project="root1",
        type="training", created=datetime.utcnow(),
        models=Models(output=[ModelItem(name="output", model="out-model", updated=datetime.utcnow())]),
    )
    task.save()

    call = FakeCall(identity(company, "stranger"))
    call.requested_endpoint_version = PartialVersion("2.9")
    with pytest.raises(errors.bad_request.InvalidTaskId):
        svc.get_by_task_id(call, company, ModelsTaskRequest(task="task1"))
