# apiserver/tests/services/test_run_import.py
import hashlib
import io
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest

from apiserver.apierrors import errors
from apiserver.bll import datachunks, run_import
from apiserver.database.model.auth import Role
from apiserver.database.model.datachunk import Datachunk, DatachunkKind, DatachunkState
from apiserver.database.model.project import Project, ProjectVisibility
from apiserver.database.model.task.task import Task
from apiserver.service_repo.auth import Identity
from apiserver.tests.db import ensure_db

ensure_db()

COMPANY = "c-import"
OWNER = "owner-import"
RUN = "yolo_run_20260519"
MANIFEST = "project: examples\ntask: yolo run\ncomment: from the lab\ntags: [imported]\n"


# ---- pure parsing ------------------------------------------------------------------------------------------------

def test_a_manifest_with_project_and_task_is_read():
    manifest = run_import.parse_manifest(MANIFEST)
    assert (manifest.project, manifest.task, manifest.comment, manifest.tags) == ("examples", "yolo run", "from the lab", ["imported"])


@pytest.mark.parametrize("text, field", [
    ("task: a\n", "project"),
    ("project: p\n", "task"),
    ("project: ''\ntask: a\n", "project"),
    ("project: p\ntask: a\nflavour: x\n", "flavour"),
    ("project: p\ntask: a\nformat: 2\n", "format"),
    ("project: p\ntask: a\ntags: nope\n", "tags"),
])
def test_a_bad_manifest_names_the_field(text, field):
    with pytest.raises(errors.bad_request.ValidationError) as caught:
        run_import.parse_manifest(text)
    assert caught.value._error_data.get("field") == field


def test_a_manifest_that_is_not_a_mapping_is_refused():
    with pytest.raises(errors.bad_request.ValidationError):
        run_import.parse_manifest("- a\n- b\n")


def test_the_zip_holds_one_folder_and_no_path_leaves_it():
    assert run_import.package_root([f"{RUN}/", f"{RUN}/args.yaml"]) == RUN
    with pytest.raises(errors.bad_request.ValidationError):
        run_import.package_root([f"{RUN}/a", "other/b"])
    with pytest.raises(errors.bad_request.ValidationError):
        run_import.package_root([f"{RUN}/../../etc/passwd"])
    with pytest.raises(errors.bad_request.ValidationError):
        run_import.package_root(["/abs/file"])


def test_scalars_come_from_results_csv_with_slashes_splitting_metric_and_variant():
    text = "epoch,train/box_loss,metrics/precision(B),note\n0,1.5,0.4,ok\n1,,0.6,nan\n"
    rows = run_import.read_scalars(text)
    assert {(r["iteration"], r["metric"], r["variant"], r["value"]) for r in rows} == {
        (0, "train", "box_loss", 1.5),
        (0, "metrics", "precision(B)", 0.4),
        (1, "metrics", "precision(B)", 0.6),
    }


def test_a_column_without_a_slash_is_the_value_of_its_metric():
    assert run_import.split_column("loss") == ("loss", "value")


def test_arguments_become_parameters_in_the_args_section():
    fields = run_import.hyperparams_from_args({"lr": "0.01", "layers": "[1, 2]"})
    assert fields["Args"]["lr"]["value"] == "0.01"
    assert fields["Args"]["layers"]["section"] == "Args"


def test_weights_and_exports_and_the_plot_are_artifacts():
    names = [f"{RUN}/weights/best.pt", f"{RUN}/espdl/model.espdl", f"{RUN}/results.png", f"{RUN}/args.yaml", f"{RUN}/weights/"]
    planned = run_import.planned_artifacts(RUN, names)
    assert [(p.name, p.artifact_type) for p in planned] == [
        ("weights/best.pt", "model"), ("espdl/model.espdl", "file"), ("results.png", "file"),
    ]


# ---- the import, against MongoDB and a fake object store ---------------------------------------------------------

class FakeBody:
    def __init__(self, data):
        self._data = data

    def iter_chunks(self, chunk_size):
        for start in range(0, len(self._data), chunk_size):
            yield self._data[start:start + chunk_size]


class FakeStore:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        return {"Body": FakeBody(self.objects[(Bucket, Key)])}

    def upload_fileobj(self, fileobj, Bucket, Key):
        self.objects[(Bucket, Key)] = fileobj.read()

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


class FakeEvents:
    def __init__(self, errors_count=0):
        self.events = []
        self.errors_count = errors_count

    def add_events(self, company_id, identity, events, worker):
        self.events.extend(events)
        return len(events), self.errors_count, {}

    def delete_task_events(self, company_id, task_ids, wait_for_delete, model=False):
        return None


def build_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def run_zip(**extra):
    files = {
        f"{RUN}/cairnml-import.yaml": MANIFEST,
        f"{RUN}/args.yaml": "lr: 0.01\nepochs: 3\n",
        f"{RUN}/results.csv": "epoch,metrics/mAP50,train/loss\n0,0.1,2.0\n1,0.2,1.5\n",
        f"{RUN}/weights/best.pt": b"weights-bytes",
        f"{RUN}/results.png": b"png-bytes",
    }
    files.update(extra)
    return build_zip(files)


@pytest.fixture
def world(monkeypatch):
    Project(id="proj-import", company=COMPANY, user=OWNER, name="examples", basename="examples",
            created=datetime.utcnow(), visibility=ProjectVisibility.private).save()
    store = FakeStore()
    events = FakeEvents()
    monkeypatch.setattr(datachunks, "_internal", lambda: store)
    monkeypatch.setattr(run_import, "event_bll", events)
    yield SimpleNamespace(store=store, events=events)
    Task.objects(company=COMPANY).delete()
    Datachunk.objects(company=COMPANY).delete()
    Project.objects(company=COMPANY).delete()


def stage(store, data, user=OWNER):
    chunk_id = f"stage{len(store.objects)}{abs(hash(data)) % 10**6}"
    key = f"_imports/{chunk_id}/run.zip"
    store.objects[(DatachunkKind.artifact.value, key)] = data
    Datachunk(
        id=chunk_id, company=COMPANY, user=user, task="", project="", kind=DatachunkKind.artifact,
        bucket=DatachunkKind.artifact.value, key=key, name="run.zip", size=len(data),
        sha256=hashlib.sha256(data).hexdigest(), state=DatachunkState.stored,
        created=datetime.utcnow(), stored=datetime.utcnow(),
    ).save()
    return chunk_id


def identity(user=OWNER):
    return Identity(user=user, company=COMPANY, role=Role.user)


def test_an_import_creates_a_new_task_with_its_parameters_scalars_and_files(world):
    chunk_id = stage(world.store, run_zip())

    result = run_import.run_import(COMPANY, identity(), chunk_id)

    task = Task.objects(id=result["id"]).get()
    assert (task.name, task.project, task.company, task.comment) == ("yolo run", "proj-import", COMPANY, "from the lab")
    assert task.status == "completed"
    assert task.hyperparams["Args"]["lr"].value == "0.01"
    assert sorted(task.execution.artifacts) and len(task.execution.artifacts) == 2
    uris = sorted(a.uri for a in task.execution.artifacts.values())
    assert uris == [f"datachunk://artifact/{task.id}/results.png", f"datachunk://artifact/{task.id}/weights/best.pt"]
    assert result["scalars"] == 4
    assert {e["metric"] for e in world.events.events} == {"metrics", "train"}
    assert Datachunk.objects(task=task.id, state=DatachunkState.stored).count() == 2


def test_a_second_import_creates_a_second_task_and_never_overwrites(world):
    first = run_import.run_import(COMPANY, identity(), stage(world.store, run_zip()))
    second = run_import.run_import(COMPANY, identity(), stage(world.store, run_zip()))
    assert first["id"] != second["id"]
    assert Task.objects(company=COMPANY, name="yolo run").count() == 2


def test_a_missing_project_stops_the_import_and_creates_nothing(world):
    data = run_zip(**{f"{RUN}/cairnml-import.yaml": "project: nowhere\ntask: x\n"})
    with pytest.raises(errors.bad_request.InvalidId):
        run_import.run_import(COMPANY, identity(), stage(world.store, data))
    assert Task.objects(company=COMPANY).count() == 0
    assert Datachunk.objects(company=COMPANY, task__ne="").count() == 0


def test_a_failed_scalar_write_leaves_no_task_and_no_file_and_keeps_the_upload(world):
    world.events.errors_count = 1
    chunk_id = stage(world.store, run_zip())
    with pytest.raises(errors.bad_request.ValidationError):
        run_import.run_import(COMPANY, identity(), chunk_id)
    assert Task.objects(company=COMPANY).count() == 0
    assert Datachunk.objects(company=COMPANY, task__ne="").count() == 0
    assert not [key for key in world.store.objects if not key[1].startswith("_imports/")]
    assert Datachunk.objects(id=chunk_id).count() == 1


def test_an_upload_of_another_user_cannot_be_imported(world):
    chunk_id = stage(world.store, run_zip(), user="someone-else")
    with pytest.raises(errors.bad_request.InvalidId):
        run_import.run_import(COMPANY, identity(), chunk_id)


def test_a_zip_without_a_manifest_is_refused(world):
    data = build_zip({f"{RUN}/results.csv": "epoch,loss\n0,1\n"})
    with pytest.raises(errors.bad_request.ValidationError):
        run_import.run_import(COMPANY, identity(), stage(world.store, data))


def test_a_run_without_results_csv_imports_with_a_warning(world):
    data = build_zip({f"{RUN}/cairnml-import.yaml": MANIFEST})
    result = run_import.run_import(COMPANY, identity(), stage(world.store, data))
    assert result["scalars"] == 0
    assert result["warnings"] and "results.csv" in result["warnings"][0]


def test_a_staged_zip_that_is_never_imported_is_removed_after_its_retention(world):
    chunk_id = stage(world.store, run_zip())
    Datachunk.objects(id=chunk_id).update(set__created=datetime(2020, 1, 1))
    assert run_import.purge_unimported() == 1
    assert Datachunk.objects(id=chunk_id).count() == 0
    assert not world.store.objects
