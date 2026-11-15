"""user_access.get_all declares every key it returns, and no other."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apiserver.database.model.auth import AuthUser
from apiserver.database.model.project import Project
from apiserver.database.model.user_access import UserAccess
from apiserver.schema import SchemaReader
from apiserver.service_repo import ServiceRepo
from apiserver.services import user_access as svc
from apiserver.tests.db import ensure_db
from apiserver.utilities.partial_version import PartialVersion

ensure_db()


@pytest.fixture
def company():
    company = f"c-ua-{uuid4().hex[:8]}"
    now = datetime.utcnow()
    AuthUser(id="u1", name="U", company=company, role="user", created=now).save()
    Project(id=f"p-{company}", company=company, user="u1", name="P", basename="P", created=now, last_update=now).save()
    UserAccess(
        id=f"a-{company}",
        company=company,
        user="u1",
        scope="company",
        kind="app",
        app_id="1",
        label="App",
        host="github.com",
        path="",
        secret="x",
        created=now,
    ).save()
    yield company
    for model in (UserAccess, Project, AuthUser):
        model.objects(company=company).delete()


def test_response_keys_equal_the_schema(company):
    conf = SchemaReader.read_file(SchemaReader.root / "user_access.conf")
    declared = set(conf["get_all"]["2.38"]["response"]["properties"])
    model = ServiceRepo._get_endpoint("user_access.get_all", PartialVersion("2.38")).request_data_model
    request = model() if model else None
    call = SimpleNamespace(data={}, identity=SimpleNamespace(user="u1", role="user"), result=SimpleNamespace(data=None))
    svc.get_all(call, company, request)
    assert set(call.result.data) == declared
    assert call.result.data["projects"] and call.result.data["company_apps"]
