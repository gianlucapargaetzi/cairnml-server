"""Section 7 of docs/e2e_testing/E2E.md: datasets and models, created on one machine and used on another.

Each step runs in its own process, so a dataset or model is only as good as what the server and the stores kept.
A dataset is a git repository whose files are in the server's dataset bucket; its version is a commit.
"""

import subprocess

import pytest

from support import workspace
from support.workspace import new_name, run_json


@pytest.fixture(scope="module")
def origin(tmp_path_factory):
    """A bare git repository standing in for the dataset's repository on GitHub."""
    path = tmp_path_factory.mktemp("dataset-origin") / "acme" / "cars-data.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def dataset(client_home, origin, tmp_path_factory):
    work = tmp_path_factory.mktemp("dataset-source") / "cars-data"
    made = run_json(client_home, "dataset_make.py", [
        "--origin", str(origin), "--work", str(work), "--unpushed",
    ], timeout=300)
    return {"made": made, "origin": str(origin)}


def fetch(client_home, dataset, commit, extra=()):
    return run_json(client_home, "dataset_fetch.py", [
        "--repo", dataset["origin"], "--commit", commit, *extra,
    ], timeout=300)


def test_dataset_is_published_in_two_versions(dataset):
    """A dataset is published twice from a git repository.

    Expect: The two versions have different commits, and the second no longer has the image that was removed.
    """
    made = dataset["made"]
    assert made["first"] != made["second"]
    assert "images/sample-2.bin" in made["first_files"]
    assert "images/sample-2.bin" not in made["second_files"]
    assert "images/sample-3.bin" in made["second_files"]


def test_second_machine_gets_the_same_files_and_hashes(client_home, dataset):
    """A second machine gets the same files and hashes.

    Expect: The files fetched in another process equal the version's list, names and hashes, for both versions.
    """
    made = dataset["made"]
    assert fetch(client_home, dataset, made["first"])["files"] == made["first_files"]
    assert fetch(client_home, dataset, made["second"])["files"] == made["second_files"]


def test_an_older_version_keeps_its_deleted_file(client_home, dataset):
    """An older version keeps a file that a later version removed.

    Expect: The first commit still has sample 2 after the second commit removed it.
    """
    made = dataset["made"]
    assert "images/sample-2.bin" in fetch(client_home, dataset, made["first"])["files"]


def test_a_run_records_the_dataset_it_used(client_home, dataset, project):
    """A run records the dataset it used.

    Expect: The run has the repository, the commit and the content hash of the files under its Datasets parameters.
    """
    made = dataset["made"]
    ran = fetch(client_home, dataset, made["second"], ["--project", project, "--name", new_name("dataset-run"),
                                                       "--alias", "cars"])
    api = workspace.device_api(client_home)
    params = api.authed("tasks.get_by_id", {"task": ran["task_id"]})["task"]["hyperparams"]["Datasets"]
    assert params["cars/repo"]["value"] == dataset["origin"]
    assert params["cars/commit"]["value"] == made["second"]
    assert params["cars/content_hash"]["value"] == ran["content_hash"]


def test_a_commit_that_was_not_pushed_is_refused(client_home, dataset, tmp_path):
    """A commit that was not pushed is refused.

    Expect: Fetching a commit that only exists on the author's machine exits with a non-zero code.
    """
    result = workspace.run_raw(client_home, "dataset_fetch.py", [
        "--repo", dataset["origin"], "--commit", dataset["made"]["third"],
    ])
    assert result.returncode != 0
    assert "is not on" in result.output


def test_registration_is_refused_when_the_server_has_no_registry(client_home):
    """The registration endpoint refuses a dataset when the server has no GitHub App and no registry.

    Expect: `dataset_registry.register` answers 400 with a message that the server is not configured.
    """
    from support.api import ApiError

    api = workspace.device_api(client_home)
    with pytest.raises(ApiError) as refused:
        api.authed("dataset_registry.register", {"repository": "https://github.com/acme/cars-data"})
    assert refused.value.status == 400
    assert "not configured" in refused.value.message


def test_dataset_fetch_of_an_unknown_commit_fails(client_home, dataset):
    """Fetching an unknown commit fails.

    Expect: The fetch exits with a non-zero code.
    """
    result = workspace.run_raw(client_home, "dataset_fetch.py", ["--repo", dataset["origin"], "--commit", "0" * 40])
    assert result.returncode != 0


@pytest.fixture(scope="module")
def model(client_home, project, tmp_path_factory):
    work = tmp_path_factory.mktemp("model-source")
    published = run_json(client_home, "model_publish.py", [
        "--project", project, "--name", new_name("model"), "--work", str(work),
    ], timeout=300)
    return published


def test_model_is_listed_under_its_run(client_home, model):
    """A model is listed under the run that produced it.

    Expect: The run's output models include the published model.
    """
    api = workspace.device_api(client_home)
    record = api.authed("tasks.get_by_id", {"task": model["task_id"]})["task"]
    models = record.get("models", {}).get("output", [])
    assert model["model_id"] in [m["model"] for m in models]


def test_model_downloads_intact_on_another_machine(client_home, model):
    """A model downloads intact on another machine.

    Expect: The digest of the downloaded model equals the digest it was published with.
    """
    fetched = run_json(client_home, "model_fetch.py", ["--model-id", model["model_id"]], timeout=300)
    assert fetched["digest"] == model["digest"]
