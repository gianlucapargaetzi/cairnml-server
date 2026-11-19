# The request's include_stats_filter is not changed by the statistics calls (tracking issue #267, step 10, issue #281).
# services/projects.py passes the same filter object to calc_own_datasets and get_project_dataset_stats, so the test
# makes the same two calls with the same dict. It needs MongoDB (see apiserver/tests/db.py).
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import pytest

from apiserver.bll.project.project_bll import ProjectBLL, dataset_tag
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.tests.db import ensure_db

ensure_db()


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


def make_project(company, id_, parent=None, system_tags=None, tags=None):
    project = Project(
        id=id_,
        company=company,
        user="owner1",
        name=id_,
        basename=id_,
        created=datetime.utcnow(),
        visibility=ProjectVisibility.public,
        parent=parent.id if parent else None,
        path=[*(parent.path or []), parent.id] if parent else [],
        system_tags=system_tags or [],
        tags=tags or [],
    )
    project.save()
    return project


def test_the_request_filter_is_not_changed_by_the_statistics_calls(company):
    suffix = uuid4().hex[:8]
    root = make_project(company, f"root-{suffix}")
    make_project(company, f"ds-{suffix}", parent=root, system_tags=[dataset_tag], tags=["t"])
    request_filter = {"system_tags": ["x"], "tags": ["t"]}
    before = deepcopy(request_filter)

    own = ProjectBLL.calc_own_datasets(company, [root.id], filter_=request_filter)
    stats, _ = ProjectBLL.get_project_dataset_stats(company, [root.id], include_children=False, filter_=request_filter)

    assert request_filter == before
    assert own == {root.id: {"own_datasets": 1}}
    assert stats[root.id] == {"datasets": {"count": 1, "tags": ["t"]}}
