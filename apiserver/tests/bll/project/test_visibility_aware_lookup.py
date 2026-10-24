from datetime import datetime

import pytest

from apiserver.apierrors import errors
from apiserver.tests.db import ensure_db
from apiserver.bll.project.project_bll import ProjectBLL
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.service_repo.auth import Identity

ensure_db()

COMPANY = "c1"
FIXTURE_NAMES = ["Foo", "Bar", "Baz", "Baz/Sub", "Qux"]
FIXTURE_IDS = ["hidden1", "public1", "hidden2", "hidden3"]


def identity(user):
    return Identity(user=user, company=COMPANY, role=Role.user)


@pytest.fixture(autouse=True)
def clean_fixture_projects():
    # The test database is shared and not reset between runs, so remove leftovers first
    Project.objects(company=COMPANY, name__in=FIXTURE_NAMES).delete()
    Project.objects(company=COMPANY, id__in=FIXTURE_IDS).delete()
    yield


def test_creating_a_project_with_a_name_that_collides_with_an_invisible_private_project_forks_a_new_one():
    Project(
        id="hidden1", company=COMPANY, user="owner1", name="Foo", basename="Foo",
        created=datetime.utcnow(), visibility=ProjectVisibility.private,
    ).save()

    new_id = ProjectBLL.create(user="owner2", company=COMPANY, name="Foo", identity=identity("owner2"))

    assert new_id != "hidden1"
    created = Project.objects(id=new_id).first()
    assert created.user == "owner2"
    assert created.name == "Foo"


def test_creating_a_project_with_a_name_that_collides_with_a_visible_project_is_refused():
    Project(
        id="public1", company=COMPANY, user="owner1", name="Bar", basename="Bar",
        created=datetime.utcnow(), visibility=ProjectVisibility.public,
    ).save()

    with pytest.raises(errors.bad_request.ExpectedUniqueData):
        ProjectBLL.create(user="owner2", company=COMPANY, name="Bar", identity=identity("owner2"))


def test_find_or_create_reuses_a_visible_project_with_the_same_name():
    Project(
        id="public1", company=COMPANY, user="owner1", name="Bar", basename="Bar",
        created=datetime.utcnow(), visibility=ProjectVisibility.public,
    ).save()

    found_id = ProjectBLL.find_or_create(
        user="owner2", company=COMPANY, project_name="Bar", description="",
        identity=identity("owner2"),
    )

    assert found_id == "public1"


def test_pushing_a_task_into_a_new_sub_project_path_still_forks_on_an_invisible_root():
    Project(
        id="hidden2", company=COMPANY, user="owner1", name="Baz", basename="Baz",
        created=datetime.utcnow(), visibility=ProjectVisibility.private,
    ).save()

    new_id = ProjectBLL.create(user="owner2", company=COMPANY, name="Baz/Sub", identity=identity("owner2"))

    created = Project.objects(id=new_id).first()
    assert created.parent != "hidden2"
    root = Project.objects(id=created.parent).first()
    assert root.user == "owner2"


def test_find_or_create_forks_on_an_invisible_collision_too():
    Project(
        id="hidden3", company=COMPANY, user="owner1", name="Qux", basename="Qux",
        created=datetime.utcnow(), visibility=ProjectVisibility.private,
    ).save()

    found_id = ProjectBLL.find_or_create(
        user="owner2", company=COMPANY, project_name="Qux", description="",
        identity=identity("owner2"),
    )

    assert found_id != "hidden3"
    created = Project.objects(id=found_id).first()
    assert created.user == "owner2"
