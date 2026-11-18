"""Endpoint handlers take (call, company, request), the way the dispatcher calls them.

The decorator refuses a handler with another signature when the module is imported, so a handler cannot reach a request
with the wrong arguments. The tests below check the refusal, and that every registered endpoint and the handlers that
were migrated from the old one-argument signature are called with the dispatcher's arguments and answer."""

from inspect import signature
from types import SimpleNamespace

import pytest

from apiserver.database.model.auth import Role
from apiserver.service_repo import API_VERSION, APICall, endpoint
from apiserver.service_repo.auth import Identity
from apiserver.service_repo.service_repo import ServiceRepo
from apiserver.utilities.partial_version import PartialVersion
from apiserver.apimodels.users import CreateRequest

# the services import each other in a fixed order, which ServiceRepo.load() follows
ServiceRepo.load()

from apiserver.bll.statistics.stats_reporter import StatisticsReporter  # noqa: E402
from apiserver.services import queues as queues_svc  # noqa: E402
from apiserver.services import users as users_svc  # noqa: E402

ROOT_IDENTITY = Identity(user="root-user", company="company-1", role=Role.root)


def _registered_endpoints():
    return [(ep.name, ep) for eps in ServiceRepo._endpoints.values() for ep in eps]


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(ServiceRepo, "_endpoints", {})
    return ServiceRepo


@pytest.mark.parametrize(
    "name, ep",
    _registered_endpoints(),
    ids=[f"{ep.name}@{ep.min_version}" for _, ep in _registered_endpoints()],
)
def test_every_registered_handler_takes_call_company_and_request(name, ep):
    signature(ep.func).bind(None, None, None)


def test_decorator_names_the_endpoint_and_handler_it_refuses(registry):
    with pytest.raises(
        TypeError, match=r"endpoint debug\.ping: handler .*legacy_ping must take \(call, company, request\)"
    ):

        @endpoint("debug.ping")
        def legacy_ping(call):
            return None

    assert "debug.ping" not in registry._endpoints


def test_decorator_accepts_a_handler_with_varargs(registry):
    @endpoint("debug.ping")
    def ping(*args):
        return None

    assert registry._endpoints["debug.ping"][0].func is ping


def _call(data=None, data_model_cls=None):
    call = APICall("test.call", data=data or {}, headers={}, remote_addr="127.0.0.1")
    call.auth = SimpleNamespace(identity=ROOT_IDENTITY)
    if data_model_cls:
        call.data_model_cls = data_model_cls
    return call


def _dispatch(name, call):
    """Calls the registered handler the way ServiceRepo.handle_call does."""
    ep = ServiceRepo._get_endpoint(name, PartialVersion(API_VERSION))
    if ep.response_data_model:
        call.result.data_model_cls = ep.response_data_model
    return ep.func(call, call.identity.company, call.data_model)


def test_server_info_answers_with_the_build_fields():
    call = _call()
    _dispatch("server.info", call)

    assert set(call.result.data) == {"version", "build", "commit", "api_version"}
    assert call.result.data["api_version"] == str(ServiceRepo.max_endpoint_version())


def test_server_get_stats_is_called_with_the_company(monkeypatch):
    monkeypatch.setattr(StatisticsReporter, "get_statistics", staticmethod(lambda company_id: {"company": company_id}))
    call = _call()
    _dispatch("server.get_stats", call)

    assert call.result.data == {"company": "company-1"}


def test_server_endpoints_answers_with_the_registry_summary():
    call = _call()
    _dispatch("server.endpoints", call)

    assert "server.info" in call.result.data["endpoints"]


def test_queues_get_default_answers_with_the_default_queue(monkeypatch):
    monkeypatch.setattr(
        queues_svc.queue_bll,
        "get_default",
        lambda company_id: SimpleNamespace(id=f"{company_id}-default", name="default"),
    )
    call = _call()
    _dispatch("queues.get_default", call)

    assert call.result.data_model.id == "company-1-default"
    assert call.result.data_model.name == "default"


def test_users_create_passes_the_request_model_to_the_business_logic(monkeypatch):
    created = []
    monkeypatch.setattr(users_svc.UserBLL, "create", staticmethod(lambda request: created.append(request)))
    call = _call(data={"id": "u1", "name": "user one", "company": "company-1"}, data_model_cls=CreateRequest)
    _dispatch("users.create", call)

    assert len(created) == 1
    assert isinstance(created[0], CreateRequest)
    assert created[0].id == "u1"
