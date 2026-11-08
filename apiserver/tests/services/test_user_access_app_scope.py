# apiserver/tests/services/test_user_access_app_scope.py
"""Only an admin may add the company's GitHub App. A user who tries is refused as a forbidden role, not with an error
of the server (the errors module must be imported where the refusal is raised)."""
from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.database.model.auth import Role
from apiserver.service_repo.auth import Identity
from apiserver.services import user_access as svc


class FakeCall:
    def __init__(self, role, data):
        self.identity = Identity(user="u1", company="c1", role=role)
        self.data = data
        self.result = SimpleNamespace(data=None, data_model=None)


def test_a_user_may_not_add_the_company_app(monkeypatch):
    monkeypatch.setattr(svc.access_bll, "add_app", lambda *args, **kwargs: pytest.fail("the App was added"))
    call = FakeCall(Role.user, {"scope": "company", "label": "App", "app_id": "1", "key": "k"})

    with pytest.raises(errors.forbidden.RoleNotAllowed):
        svc.add_app(call, "c1", None)
