"""The old sharing fields are gone: the share records (bll/access) are the only source of who may use or see a resource.
Dataset.visibility stays: it is the user / projects / Public value, not a grant list."""

from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project
from apiserver.database.model.queue import Queue
from apiserver.database.model.user_access import UserAccess


def test_the_four_models_no_longer_define_the_old_sharing_fields():
    assert "users" not in Queue._fields
    assert "queues" not in Project._fields
    assert "projects" not in Dataset._fields
    assert "granted_projects" not in UserAccess._fields
    assert "visibility" in Dataset._fields
