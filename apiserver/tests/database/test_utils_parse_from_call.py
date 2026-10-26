from datetime import datetime

import pytest

from apiserver.apierrors import errors
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import Task
from apiserver.database.utils import parse_from_call
from apiserver.tests.db import ensure_db

ensure_db()

COMPANY = "c1"


def make_project() -> Project:
    project = Project(
        id="utils-p1", company=COMPANY, user="owner1", name="Utils",
        basename="Utils", created=datetime.utcnow(),
    )
    project.save()
    return project


def test_a_missing_project_id_raises_invalid_project_id():
    with pytest.raises(errors.bad_request.InvalidProjectId) as exc:
        parse_from_call({"project": "no-such-id"}, {"project": Project}, {"project"})
    assert exc.value.msg == "Invalid project id: id=no-such-id"


def test_an_existing_project_id_is_accepted():
    project = make_project()
    fields = parse_from_call({"project": project.id}, {"project": Project}, {"project"})
    assert fields == {"project": project.id}


def test_a_missing_task_id_still_raises_the_generic_error():
    """Only Project gets the InvalidProjectId treatment: task-parent visibility is unrelated to this feature."""
    with pytest.raises(errors.bad_request.FieldsValueError) as exc:
        parse_from_call({"parent": "no-such-id"}, {"parent": Task}, {"parent"})
    assert "expecting Task id" in exc.value.msg
