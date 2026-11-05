"""The dataset keys cover one dataset, at one level, and only for a caller who may use that level."""

import json
from datetime import datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from apiserver.apierrors import errors
from apiserver.bll import dataset_credentials as bll
from apiserver.database.model.dataset import Dataset
from apiserver.database.model.project import Project, ProjectMember, ProjectRole
from apiserver.database.model.share import Share
from apiserver.idp import public_origin
from apiserver.tests.db import ensure_db

ensure_db()

OWNER = "u-owner"
OTHER = "u-other"
PREFIX_TAIL = "acme/cars"


@pytest.fixture
def company():
    return f"c-{uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def clean_rows(company):
    yield
    Dataset.objects(company=company).delete()
    Share.objects(company=company).delete()
    Project.objects(company=company).delete()


@pytest.fixture
def sts(monkeypatch):
    client = MagicMock()
    client.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AK",
            "SecretAccessKey": "SK",
            "SessionToken": "ST",
            "Expiration": datetime(2026, 10, 23, 10, 36, 24),
        }
    }
    monkeypatch.setattr(bll, "_sts", lambda: client)
    monkeypatch.setattr(bll, "_ttl", lambda: 1800)
    monkeypatch.setattr(public_origin, "storage_endpoint", lambda: "http://localhost:9000")
    monkeypatch.setattr(bll.user_access, "my_projects", lambda company, user: [])
    return client


def add_dataset(company, owner=OWNER, visibility="user", projects=None, kind="repo", ref="acme/cars"):
    """A dataset with its old fields and the share records the writers keep beside them (Public, and one project share
    for each allocated project)."""
    record = Dataset(
        id=f"d-{uuid4().hex[:8]}", company=company, kind=kind, ref=ref, name=ref.split("/")[-1], owner=owner,
        visibility=visibility, projects=projects or [], created=datetime.utcnow(),
    )
    record.save()
    resource_type = "registry" if kind == "registry" else "dataset"
    if visibility == "Public":
        Share(id=uuid4().hex, company=company, resource_type=resource_type, resource_id=record.id, scope="public",
              level="read", granted_by=owner, created=datetime.utcnow()).save()
    for root in projects or []:
        Share(id=uuid4().hex, company=company, resource_type=resource_type, resource_id=record.id, scope="projects",
              project=root, level="read", granted_by=owner, created=datetime.utcnow()).save()


def issued_policy(sts) -> dict:
    return json.loads(sts.assume_role.call_args.kwargs["Policy"])


def actions_on(policy: dict, resource: str) -> set:
    return {
        action
        for statement in policy["Statement"]
        if statement["Effect"] == "Allow" and resource in statement["Resource"]
        for action in statement["Action"]
    }


def object_resources(policy: dict) -> set:
    return {r for s in policy["Statement"] for r in s["Resource"] if r.endswith("/*")}


# --- The policy, built for one prefix and level (no database)

class TestPolicy:
    def test_read_has_no_write_actions(self):
        policy = bll.session_policy("dataset/c1/acme/cars", "read")
        allowed = {a for s in policy["Statement"] for a in s["Action"]}
        assert "s3:GetObject" in allowed
        assert not allowed & {"s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"}

    def test_read_write_adds_the_write_actions_for_the_same_prefix(self):
        policy = bll.session_policy("dataset/c1/acme/cars", "read_write")
        object_actions = actions_on(policy, "arn:aws:s3:::dataset/c1/acme/cars/*")
        assert {"s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"} <= object_actions
        assert "s3:DeleteObject" not in object_actions

    def test_the_policy_names_only_the_datasets_prefix_for_objects(self):
        for level in ("read", "read_write"):
            policy = bll.session_policy("dataset/c1/acme/cars", level)
            assert object_resources(policy) == {"arn:aws:s3:::dataset/c1/acme/cars/*"}

    def test_listing_is_limited_to_the_datasets_keys(self):
        policy = bll.session_policy("dataset/c1/acme/cars", "read")
        listing = [s for s in policy["Statement"] if "s3:ListBucket" in s["Action"]]
        assert len(listing) == 1
        condition = listing[0]["Condition"]["StringLike"]["s3:prefix"]
        assert condition == ["c1/acme/cars", "c1/acme/cars/*"]

    def test_an_unknown_level_is_refused(self):
        with pytest.raises(errors.bad_request.ValidationError):
            bll.session_policy("dataset/c1/acme/cars", "admin")


# --- The call: the caller's share decides the level, and the response names the prefix

class TestDatasetCredentials:
    def test_a_read_key_for_a_visible_dataset_names_its_prefix_and_has_no_put(self, company, sts):
        add_dataset(company, owner=OWNER, visibility="Public")

        result = bll.dataset_credentials(company, OTHER, "acme/cars", "read")

        assert result["prefix"] == f"dataset/{company}/{PREFIX_TAIL}"
        assert result["bucket"] == "dataset"
        policy = issued_policy(sts)
        assert "s3:PutObject" not in {a for s in policy["Statement"] for a in s["Action"]}
        assert object_resources(policy) == {f"arn:aws:s3:::dataset/{company}/{PREFIX_TAIL}/*"}

    def test_the_owner_gets_a_read_write_key_for_the_dataset_only(self, company, sts):
        add_dataset(company, owner=OWNER)

        bll.dataset_credentials(company, OWNER, "acme/cars", "read_write")

        policy = issued_policy(sts)
        assert "s3:PutObject" in actions_on(policy, f"arn:aws:s3:::dataset/{company}/{PREFIX_TAIL}/*")
        assert object_resources(policy) == {f"arn:aws:s3:::dataset/{company}/{PREFIX_TAIL}/*"}

    def test_an_admin_may_take_read_write_on_someone_elses_dataset(self, company, sts):
        add_dataset(company, owner=OWNER)

        bll.dataset_credentials(company, OTHER, "acme/cars", "read_write", is_admin=True)

        assert "s3:PutObject" in actions_on(issued_policy(sts), f"arn:aws:s3:::dataset/{company}/{PREFIX_TAIL}/*")

    def test_a_caller_with_no_share_is_refused_and_no_key_is_issued(self, company, sts):
        add_dataset(company, owner=OWNER, visibility="user")

        with pytest.raises(errors.bad_request.ValidationError):
            bll.dataset_credentials(company, OTHER, "acme/cars", "read")
        sts.assume_role.assert_not_called()

    def test_an_unknown_repository_is_refused_like_a_hidden_one(self, company, sts):
        with pytest.raises(errors.bad_request.ValidationError):
            bll.dataset_credentials(company, OWNER, "acme/missing", "read")
        sts.assume_role.assert_not_called()

    def test_a_registry_is_not_a_dataset_bucket(self, company, sts):
        add_dataset(company, owner=OWNER, kind="registry", ref="acme/registry")

        with pytest.raises(errors.bad_request.ValidationError):
            bll.dataset_credentials(company, OWNER, "acme/registry", "read")
        sts.assume_role.assert_not_called()

    def test_a_reader_asking_for_read_write_is_refused(self, company, sts):
        add_dataset(company, owner=OWNER, visibility="Public")

        with pytest.raises(errors.forbidden.NoWritePermission):
            bll.dataset_credentials(company, OTHER, "acme/cars", "read_write")
        sts.assume_role.assert_not_called()

    def test_a_project_member_reads_a_dataset_allocated_to_the_project(self, company, sts, monkeypatch):
        Project(id="p1", company=company, user=OWNER, name="p1", basename="p1", created=datetime.utcnow(),
                members=[ProjectMember(user=OTHER, role=ProjectRole.read, added=datetime.utcnow(), added_by=OWNER)]
                ).save()
        add_dataset(company, owner=OWNER, visibility="projects", projects=["p1"])
        monkeypatch.setattr(bll.user_access, "my_projects", lambda company, user: [{"id": "p1"}])

        bll.dataset_credentials(company, OTHER, "acme/cars", "read")

        sts.assume_role.assert_called_once()

    def test_the_request_names_a_repository_on_github(self, company, sts):
        add_dataset(company, owner=OWNER)

        with pytest.raises(errors.bad_request.ValidationError):
            bll.dataset_credentials(company, OWNER, "not a repository", "read")
        sts.assume_role.assert_not_called()


# R14: a repository with no kept record is new. Its creator may write under the company prefix, and nobody may read it.

class TestNewDataset:
    def test_a_new_name_is_writable_under_the_callers_company_prefix(self, company, sts):
        bll.dataset_credentials(company, OWNER, "acme/new-one", "read_write")

        policy = issued_policy(sts)
        assert "s3:PutObject" in actions_on(policy, f"arn:aws:s3:::dataset/{company}/acme/new-one/*")
        assert object_resources(policy) == {f"arn:aws:s3:::dataset/{company}/acme/new-one/*"}

    def test_a_new_name_is_refused_at_read(self, company, sts):
        with pytest.raises(errors.bad_request.ValidationError):
            bll.dataset_credentials(company, OWNER, "acme/new-one", "read")
        sts.assume_role.assert_not_called()

    def test_an_existing_record_is_not_opened_to_a_non_owner_at_read_write(self, company, sts):
        add_dataset(company, owner=OWNER, visibility="user")

        with pytest.raises(errors.forbidden.NoWritePermission):
            bll.dataset_credentials(company, OTHER, "acme/cars", "read_write")
        sts.assume_role.assert_not_called()

    def test_a_name_held_by_another_record_in_the_company_is_not_new(self, company, sts):
        add_dataset(company, owner=OWNER, kind="registry", ref="acme/taken")

        with pytest.raises(errors.bad_request.ValidationError):
            bll.dataset_credentials(company, OTHER, "acme/taken", "read_write")
        sts.assume_role.assert_not_called()


# Names that could widen or escape the prefix are refused before any key is issued.

@pytest.mark.parametrize("repository", ["*/*", "acme/*", "../..", "acme/cars\n", "acme/c?rs", "a b/c", "./x", "acme/.", "acme/..", "a/b/c", "acme/", "/cars"])
@pytest.mark.parametrize("level", ["read", "read_write"])
def test_an_unsafe_repository_name_is_refused_before_any_key_is_issued(company, sts, repository, level):
    with pytest.raises(errors.bad_request.ValidationError):
        bll.dataset_credentials(company, OWNER, repository, level)
    sts.assume_role.assert_not_called()


def test_a_company_id_with_a_wildcard_is_refused_before_any_key_is_issued(sts):
    with pytest.raises(errors.bad_request.ValidationError):
        bll.dataset_credentials("c*", OWNER, "acme/cars", "read_write")
    sts.assume_role.assert_not_called()


def test_the_prefix_never_contains_a_wildcard(company, sts):
    bll.dataset_credentials(company, OWNER, "acme/cars-2_b.x", "read_write")

    policy = issued_policy(sts)
    assert object_resources(policy) == {f"arn:aws:s3:::dataset/{company}/acme/cars-2_b.x/*"}
    for resource in object_resources(policy):
        assert resource.count("*") == 1 and resource.endswith("/*")


# A job token acts as its task's owner (issue_job_token sets the identity user to the task's user), so the key
# request from a job is checked for that user like any other caller.

def test_a_job_token_is_checked_as_the_task_owner(company, sts):
    from types import SimpleNamespace

    from apiserver.database.model.auth import Role
    from apiserver.idp import tokens
    from apiserver.services import datachunks as svc
    from apiserver.apimodels.datachunks import DatasetCredentialsRequest
    from apiserver.service_repo.auth import Identity

    add_dataset(company, owner=OWNER, visibility="user")
    task_owner = SimpleNamespace(id=OTHER, company=company, role=Role.user, name="other")
    token = tokens.issue_job_token(task_owner, "t1", "dev1")
    token_user = tokens.read_claims(token)["identity"]["user"]
    assert token_user == OTHER

    call = SimpleNamespace(identity=Identity(user=token_user, company=company, role=Role.user),
                           result=SimpleNamespace(data=None))
    with pytest.raises(errors.forbidden.NoWritePermission):
        svc.get_dataset_credentials(call, company, DatasetCredentialsRequest(repository="acme/cars", level="read_write"))
    sts.assume_role.assert_not_called()
