"""The behaviour that API 2.41 removed, kept for the definitions older than 2.41 (the owner's model, #217).

The expected values are the ones the server gave at the tag pre-2.41 for the same inputs.
"""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.apimodels.login import GetSupportedModesRequest, GetSupportedModesResponse
from apiserver.apimodels.projects import ProjectsGetRequest
from apiserver.database.model.auth import Role
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.service_repo.auth import Identity
from apiserver.services import projects as projects_svc
from apiserver.services.login import supported_modes
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


class FakeCall:
    def __init__(self, identity=None, actual=None, data=None, auth=None):
        self.identity = identity
        self.auth = auth
        self.data = data or {}
        self.actual_endpoint_version = PartialVersion(actual) if actual else None
        self.result = SimpleNamespace(data=None, data_model=None)


def make_project(company, id_, visibility):
    Project(
        id=id_,
        company=company,
        user="owner1",
        name=id_,
        basename=id_,
        created=datetime.utcnow(),
        visibility=visibility,
    ).save()


@pytest.fixture
def companies():
    """A caller company, and a public project of no company: visible to the caller only with allow_public"""
    caller = f"c-{uuid4().hex[:8]}"
    other = f"c-{uuid4().hex[:8]}"
    public_id = f"pub-{uuid4().hex[:8]}"
    make_project("", public_id, ProjectVisibility.public)
    yield caller, other, public_id
    # the public projects are shared by every company of the test database
    Project.objects(id=public_id).delete()


def admin(company):
    return Identity(user="admin1", company=company, role=Role.admin)


def get_all_ex_ids(caller, public_id, actual, data):
    call = FakeCall(identity=admin(caller), actual=actual, data={"id": [public_id], **data})
    returned = projects_svc.get_all_ex(call, caller, ProjectsGetRequest())
    # an empty answer is returned, a non-empty one is set on the call
    result = returned if returned is not None else call.result.data
    return [p["id"] for p in result["projects"]]


@pytest.mark.parametrize("actual", ["2.23", "2.27", "2.40"])
def test_non_public_true_excludes_public_projects_of_other_companies(companies, actual):
    caller, _, public_id = companies
    assert get_all_ex_ids(caller, public_id, actual, {"non_public": True}) == []


@pytest.mark.parametrize("actual", ["2.23", "2.27", "2.40"])
def test_non_public_false_includes_public_projects_of_other_companies(companies, actual):
    caller, _, public_id = companies
    assert get_all_ex_ids(caller, public_id, actual, {"non_public": False}) == [public_id]


@pytest.mark.parametrize("actual", ["2.23", "2.27", "2.40"])
def test_allow_public_wins_over_non_public(companies, actual):
    caller, _, public_id = companies
    data = {"allow_public": True, "non_public": True}
    assert get_all_ex_ids(caller, public_id, actual, data) == [public_id]


def test_no_flag_at_an_older_definition_keeps_the_default(companies):
    caller, _, public_id = companies
    assert get_all_ex_ids(caller, public_id, "2.27", {}) == [public_id]


def test_non_public_is_ignored_at_2_41(companies):
    caller, _, public_id = companies
    assert get_all_ex_ids(caller, public_id, "2.41", {"non_public": True}) == [public_id]


def test_supported_modes_reports_server_errors_below_2_41():
    call = FakeCall(actual="2.9", auth=None)
    model = supported_modes(call, None, GetSupportedModesRequest())
    assert model.to_struct() == {
        "authenticated": False,
        "basic": {"enabled": False, "guest": {"enabled": False}},
        "server_errors": {"es_connection_error": False, "missed_es_upgrade": False},
        "sso": {},
        "sso_providers": [],
    }


def test_supported_modes_has_no_server_errors_at_2_41():
    call = FakeCall(actual="2.41", auth=None)
    model = supported_modes(call, None, GetSupportedModesRequest())
    assert isinstance(model, GetSupportedModesResponse)
    assert "server_errors" not in model.to_struct()
    assert model.to_struct() == {
        "authenticated": False,
        "basic": {"enabled": False, "guest": {"enabled": False}},
        "sso": {},
        "sso_providers": [],
    }
