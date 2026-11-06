# apiserver/tests/services/test_registry_one_time_read.py
"""A dataset registry is read from GitHub once: when it is added, and when its owner or an admin refreshes it. The page
loads (dataset_registry.get_all and get_registries, the GitHub App status) and the details of an entry read the database
only. A GitHub error refuses the add or the refresh with its reason, and writes nothing."""

import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests

from apiserver.apierrors import errors
from apiserver.apimodels.dataset_registry import RegistryRequest
from apiserver.bll import datasets as access_bll
from apiserver.bll import github_status
from apiserver.database.model.auth import Role, User
from apiserver.database.model.dataset import Dataset
from apiserver.idp.github import GitHubError
from apiserver.service_repo import ServiceRepo
from apiserver.service_repo.auth import Identity
from apiserver.services import dataset_registry as registry_svc
from apiserver.services import datasets as datasets_svc
from apiserver.tests.db import ensure_db

ensure_db()

REGISTRY_URL = "https://github.com/acme/registry"
CARS = {"splits": {"train": 50000}}  # the metadata file of the entry cars
CARS_STORED = {"modalities": {}, "labels": {}, "splits": {"train": 50000}, "samples": None}  # as the server keeps it


class FakeReader:
    """What GitHub answers for a registry: one entry file per entry, and one metadata file per entry. Every read is
    counted, and ``fail`` makes each read raise it."""

    def __init__(self, entries, metadata=None, fail=None):
        self.entries = entries  # name -> (source, link)
        self.metadata = metadata or {}  # name -> the metadata dict
        self.fail = fail
        self.reads = 0

    def read_dir_texts(self, repo, path):
        self.reads += 1
        if self.fail:
            raise self.fail
        return {f"{name}.yaml": f"source: {source}\nlink: {link}\n" for name, (source, link) in self.entries.items()}

    def read_file(self, repo, path, ref=None):
        self.reads += 1
        if self.fail:
            raise self.fail
        name = path[len("metadata/"):-len(".json")]
        return json.dumps(self.metadata[name]).encode() if name in self.metadata else None


class FakeCall:
    def __init__(self, user, role=Role.user, data=None):
        self.identity = Identity(user=user, company=COMPANY, role=role)
        self.result = SimpleNamespace(data=None, data_model=None)
        self.data = data
        self.auth = None


COMPANY = "c-identity"  # the company of the caller's identity; the services take the company as an argument


@pytest.fixture(autouse=True)
def no_github_http(monkeypatch):
    """Any request to GitHub over HTTP fails the test: the reads in these tests go through the fake reader only."""

    def refuse(*args, **kwargs):
        raise AssertionError("GitHub was called over HTTP")

    monkeypatch.setattr(requests.Session, "request", refuse)


@pytest.fixture
def company(monkeypatch):
    company_id = f"c-{uuid4().hex[:8]}"
    for user, role in (("owner", Role.user), ("stranger", Role.user), ("admin1", Role.admin)):
        User(id=f"{user}-{company_id}", name=user, company=company_id, role=role, created=datetime.utcnow()).save()
    monkeypatch.setattr(registry_svc, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(registry_svc, "client_address", lambda call: "127.0.0.1")
    monkeypatch.setattr(datasets_svc, "audit", lambda *args, **kwargs: None)
    yield company_id
    for model in (Dataset, User):
        model.objects(company=company_id).delete()


@pytest.fixture
def github(monkeypatch):
    """The reader the registry is read with: the test sets it. The credential decision is not under test here."""
    state = SimpleNamespace(reader=FakeReader({}))
    monkeypatch.setattr(access_bll, "_decide",
                        lambda company, user, project, url, repo, name: (None, state.reader, "ok", ""))
    return state


def owner(company):
    return f"owner-{company}"


def stranger(company):
    return f"stranger-{company}"


def admin(company):
    return f"admin1-{company}"


def add_registry(company, reader, github):
    github.reader = reader
    call = FakeCall(owner(company))
    registry_svc.add(call, company, RegistryRequest(repository="acme/registry"))
    return Dataset.objects(company=company, kind="registry").first()


def entry(company, name):
    return Dataset.objects(company=company, kind="registry_entry", ref=f"acme/registry/{name}").first()


def test_adding_a_registry_reads_it_once_and_stores_its_entries_and_metadata(company, github):
    reader = FakeReader({"cars": ("web", "https://example.org/cars"), "coco": ("huggingface", "hf://coco")},
                        metadata={"cars": CARS})

    registry = add_registry(company, reader, github)

    assert reader.reads == 3  # the listing, and the metadata file of each of the two entries
    assert registry.read_at is not None
    cars = entry(company, "cars")
    assert (cars.source, cars.link, cars.state) == ("web", "https://example.org/cars", "ok")
    assert json.loads(cars.metadata) == CARS_STORED
    assert cars.read_at is not None
    assert entry(company, "coco").metadata == ""  # no metadata file: stored as none


def test_page_loads_read_the_database_only(company, github):
    reader = FakeReader({"cars": ("web", "https://example.org/cars")}, metadata={"cars": CARS})
    add_registry(company, reader, github)
    reader.fail = AssertionError("a page load read GitHub")
    reads_before = reader.reads

    listing = FakeCall(owner(company))
    registry_svc.get_all(listing, company, None)
    registries = FakeCall(owner(company))
    registry_svc.get_registries(registries, company, None)

    assert [row["name"] for row in listing.result.data["datasets"]] == ["cars"]
    assert [row["repository"] for row in registries.result.data["registries"]] == ["acme/registry"]
    assert reader.reads == reads_before


def test_details_of_an_entry_show_its_stored_metadata_and_call_no_github(company, github, monkeypatch):
    reader = FakeReader({"cars": ("web", "https://example.org/cars")}, metadata={"cars": CARS})
    add_registry(company, reader, github)
    reader.fail = AssertionError("the details read GitHub")

    def no_reader():
        raise AssertionError("the details of a registry entry asked for a GitHub reader")

    monkeypatch.setattr(datasets_svc.github_check, "reader", no_reader)
    call = FakeCall(owner(company), data={"registry_name": "cars"})

    datasets_svc.get_details(call, company, None)

    details = call.result.data
    assert details["kind"] == "registry"
    assert details["registry"] == "acme/registry"
    assert details["source"] == "web"
    assert details["link"] == "https://example.org/cars"
    assert details["metadata"] == CARS_STORED


def test_a_details_request_for_an_entry_that_is_not_stored_says_so(company):
    call = FakeCall(owner(company), data={"registry_name": "nothing-here"})

    with pytest.raises(errors.bad_request.ValidationError) as refused:
        datasets_svc.get_details(call, company, None)

    assert "no dataset nothing-here in the registries of this server" in refused.value.msg


def test_refresh_adds_updates_and_removes_entries_and_counts_them(company, github):
    reader = FakeReader({"cars": ("web", "https://example.org/cars"), "coco": ("huggingface", "hf://coco")},
                        metadata={"cars": CARS})
    registry = add_registry(company, reader, github)
    reader.entries = {"cars": ("web", "https://example.org/cars-v2"), "fashion": ("huggingface", "hf://fashion")}
    call = FakeCall(owner(company), data={"id": registry.id})

    registry_svc.refresh(call, company, None)

    assert call.result.data == {"added": 1, "updated": 1, "removed": 1}
    assert entry(company, "cars").link == "https://example.org/cars-v2"
    assert entry(company, "fashion") is not None
    assert entry(company, "coco") is None


def test_an_admin_may_refresh_a_registry_of_another_user(company, github):
    reader = FakeReader({"cars": ("web", "https://example.org/cars")}, metadata={"cars": CARS})
    registry = add_registry(company, reader, github)
    reader.entries = {}
    call = FakeCall(admin(company), role=Role.admin, data={"id": registry.id})

    registry_svc.refresh(call, company, None)

    assert call.result.data == {"added": 0, "updated": 0, "removed": 1}
    assert entry(company, "cars") is None


def test_a_user_who_is_not_the_owner_cannot_refresh_and_nothing_is_read(company, github):
    reader = FakeReader({"cars": ("web", "https://example.org/cars")}, metadata={"cars": CARS})
    registry = add_registry(company, reader, github)
    reads_before = reader.reads

    with pytest.raises(errors.bad_request.ValidationError):
        registry_svc.refresh(FakeCall(stranger(company), data={"id": registry.id}), company, None)

    assert reader.reads == reads_before
    assert entry(company, "cars").link == "https://example.org/cars"


def test_a_github_error_on_add_refuses_it_with_the_reason_and_writes_nothing(company, github):
    reader = FakeReader({"cars": ("web", "https://example.org/cars")}, fail=GitHubError("rate limit exceeded"))
    github.reader = reader

    with pytest.raises(errors.bad_request.ValidationError) as refused:
        registry_svc.add(FakeCall(owner(company)), company, RegistryRequest(repository="acme/registry"))

    assert "rate limit exceeded" in refused.value.msg
    assert Dataset.objects(company=company).count() == 0


def test_a_metadata_file_that_is_not_json_refuses_the_add_and_writes_nothing(company, github):
    reader = FakeReader({"cars": ("web", "https://example.org/cars")})
    reader.read_file = lambda repo, path, ref=None: b"not json"
    github.reader = reader

    with pytest.raises(errors.bad_request.ValidationError):
        registry_svc.add(FakeCall(owner(company)), company, RegistryRequest(repository="acme/registry"))

    assert Dataset.objects(company=company).count() == 0


def test_a_github_error_on_refresh_refuses_it_and_keeps_the_stored_data(company, github):
    reader = FakeReader({"cars": ("web", "https://example.org/cars")}, metadata={"cars": CARS})
    registry = add_registry(company, reader, github)
    reader.fail = GitHubError("server error")
    count_before = Dataset.objects(company=company).count()

    with pytest.raises(errors.bad_request.ValidationError) as refused:
        registry_svc.refresh(FakeCall(owner(company), data={"id": registry.id}), company, None)

    assert "server error" in refused.value.msg
    assert Dataset.objects(company=company).count() == count_before
    cars = entry(company, "cars")
    assert cars.link == "https://example.org/cars"
    assert json.loads(cars.metadata) == CARS_STORED


def test_the_status_check_counts_the_stored_entries_and_reads_no_registry(company, github, monkeypatch):
    reader = FakeReader({"cars": ("web", "https://example.org/cars"), "coco": ("huggingface", "hf://coco")})
    registry = add_registry(company, reader, github)

    class App:
        def app_info(self):
            return {"slug": "cairnml"}

        def installed_repositories(self):
            return []

    def must_not_list(*args, **kwargs):
        raise AssertionError("the status check listed a registry on GitHub")

    monkeypatch.setattr(github_status, "build_from_config", lambda: App())
    monkeypatch.setattr("apiserver.bll.dataset_registry.list_entries", must_not_list)

    result = github_status.run_check()

    [row] = [r for r in result["registries"] if r["repository"] == registry.ref]
    assert row["ok"] is True
    assert row["datasets"] == 2


def test_the_refresh_endpoint_is_registered():
    ServiceRepo.load()

    assert "dataset_registry.refresh" in ServiceRepo.endpoint_names()
