"""Import of a run that was not produced by CairnML, from a standard ZIP.

The ZIP is uploaded to the object store as a staging upload (see datachunks.prepare_upload with staging) and then
imported. The import creates one new task in the project named by the manifest, with the run's parameters, its scalars
and its files attached. It never overwrites an existing task: a second import of the same ZIP creates a second task.

The ZIP has one top-level folder, the run, with the files below (all but the manifest are optional):

    <run>/cairnml-import.yaml   the manifest: project and task (required), comment and tags (optional)
    <run>/args.yaml             training arguments, stored as parameters in the "Args" section
    <run>/results.csv           per-epoch metrics, stored as scalars (the "epoch" column is the iteration)
    <run>/weights/...           checkpoints, stored as artifacts of type model
    <run>/espdl/...             deployment exports, stored as artifacts of type file
    <run>/results.png           the training plot, stored as an artifact of type file

A failed import leaves nothing behind: the task, its scalars and the files it stored are removed. The staging upload
stays, so the import can be retried; an unimported staging upload is removed after STAGING_MAX_AGE.
"""

import csv
import hashlib
import io
import json
import math
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence, Tuple
from urllib.parse import quote

import yaml
from botocore.exceptions import ClientError

from apiserver.apierrors import errors
from apiserver.bll import datachunks
from apiserver.bll.event import EventBLL
from apiserver.bll.organization import OrgBLL, Tags
from apiserver.bll.project.access import ProjectAccessBLL
from apiserver.bll.task.artifacts import get_artifact_id
from apiserver.bll.task.param_utils import params_prepare_for_save
from apiserver.bll.util import update_project_time
from apiserver.database.model.datachunk import Datachunk, DatachunkKind, DatachunkState
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import (
    Artifact,
    Execution,
    Task,
    TaskStatus,
    TaskType,
)
from apiserver.database.utils import id as create_id
from apiserver.es_factory import es_factory
from apiserver.service_repo.auth import Identity

MANIFEST_NAME = "cairnml-import.yaml"
ARGS_NAME = "args.yaml"
SCALARS_NAME = "results.csv"
PLOT_NAME = "results.png"
WEIGHTS_DIR = "weights/"
EXPORTS_DIR = "espdl/"
ARGS_SECTION = "Args"
MANIFEST_FORMAT = 1
MANIFEST_FIELDS = frozenset({"format", "project", "task", "comment", "tags"})
MAX_NAME_LENGTH = 256
STAGING_MAX_AGE = timedelta(hours=24)
COPY_BLOCK = 8 * 1024**2

event_bll = EventBLL()
org_bll = OrgBLL()


@dataclass
class Manifest:
    project: str
    task: str
    comment: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class Planned:
    """A file of the run that is stored as an artifact."""

    name: str
    member: str
    artifact_type: str


def parse_manifest(text: str) -> Manifest:
    """Reads and checks the manifest. The error names the field that is wrong."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise errors.bad_request.ValidationError("manifest: not valid YAML", field=MANIFEST_NAME) from None
    if not isinstance(data, dict):
        raise errors.bad_request.ValidationError("manifest: must be a mapping of fields", field=MANIFEST_NAME)

    unknown = sorted(set(data) - MANIFEST_FIELDS)
    if unknown:
        raise errors.bad_request.ValidationError("manifest: unknown field", field=unknown[0], fields=unknown)
    if data.get("format", MANIFEST_FORMAT) != MANIFEST_FORMAT:
        raise errors.bad_request.ValidationError("manifest: format must be 1", field="format")

    names = {}
    for name in ("project", "task"):
        value = data.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_NAME_LENGTH:
            raise errors.bad_request.ValidationError(
                f"{name}: required, a name of 1 to {MAX_NAME_LENGTH} characters", field=name
            )
        names[name] = value.strip()

    comment = data.get("comment", "") or ""
    if not isinstance(comment, str):
        raise errors.bad_request.ValidationError("comment: must be text", field="comment")
    tags = data.get("tags", []) or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise errors.bad_request.ValidationError("tags: must be a list of text", field="tags")
    return Manifest(project=names["project"], task=names["task"], comment=comment, tags=list(tags))


def package_root(names: Sequence[str]) -> str:
    """The top-level folder of the ZIP. A ZIP must hold one folder, and no entry may leave it."""
    roots = set()
    for name in names:
        if not name or name.startswith("/") or "\\" in name or any(part == ".." for part in name.split("/")):
            raise errors.bad_request.ValidationError("the ZIP holds an unsafe path", path=name)
        roots.add(name.split("/", 1)[0])
    if len(roots) != 1:
        raise errors.bad_request.ValidationError("the ZIP must hold one folder, the run", folders=sorted(roots))
    return roots.pop()


def split_column(column: str) -> Tuple[str, str]:
    """(metric, variant) for a results.csv column. "train/box_loss" is metric "train", variant "box_loss"; a column
    without a slash is metric and variant "value"."""
    column = column.strip()
    if "/" in column:
        metric, _, variant = column.partition("/")
        return metric, variant
    return column, "value"


def read_scalars(text: str) -> List[dict]:
    """(iteration, metric, variant, value) for each number in results.csv. Empty and non-numeric cells are skipped."""
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for index, row in enumerate(reader):
        iteration = index
        epoch = row.get("epoch")
        if epoch not in (None, ""):
            try:
                iteration = int(float(epoch))
            except ValueError:
                raise errors.bad_request.ValidationError("results.csv: epoch must be a number", field=SCALARS_NAME) from None
        for column, raw in row.items():
            if column is None or column.strip() == "epoch" or raw in (None, ""):
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if not math.isfinite(value):
                continue
            metric, variant = split_column(column)
            rows.append({"iteration": iteration, "metric": metric, "variant": variant, "value": value})
    return rows


def read_args(text: str) -> Dict[str, str]:
    """The training arguments as text values. Nested values are kept as JSON text."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        raise errors.bad_request.ValidationError("args.yaml: not valid YAML", field=ARGS_NAME) from None
    if not isinstance(data, dict):
        raise errors.bad_request.ValidationError("args.yaml: must be a mapping", field=ARGS_NAME)
    return {
        str(key): value if isinstance(value, str) else json.dumps(value, default=str)
        for key, value in data.items()
    }


def hyperparams_from_args(args: Dict[str, str]) -> dict:
    section = {
        name: {"section": ARGS_SECTION, "name": name, "value": value, "type": "str"}
        for name, value in args.items()
    }
    fields = {"hyperparams": {ARGS_SECTION: section}} if section else {}
    params_prepare_for_save(fields)
    return fields.get("hyperparams", {})


def planned_artifacts(root: str, names: Sequence[str]) -> List[Planned]:
    """The files to store as artifacts, by their name inside the run (for example "weights/best.pt")."""
    planned = []
    for member in names:
        if member.endswith("/"):
            continue
        relative = member[len(root) + 1:]
        if relative.startswith(WEIGHTS_DIR):
            planned.append(Planned(relative, member, "model"))
        elif relative.startswith(EXPORTS_DIR) or relative == PLOT_NAME:
            planned.append(Planned(relative, member, "file"))
    return planned


def _copy_and_hash(zf: zipfile.ZipFile, member: str) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with zf.open(member) as stream:
        for block in iter(lambda: stream.read(COPY_BLOCK), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _project_for(company: str, name: str) -> Project:
    projects = list(Project.objects(company=company, name=name).only("id"))
    if not projects:
        raise errors.bad_request.InvalidId("project not found: no project with this name", project=name)
    if len(projects) > 1:
        raise errors.bad_request.ValidationError("more than one project has this name", project=name)
    return projects[0]


def _staged_for(company: str, user: str, chunk_id: str) -> Datachunk:
    chunk = Datachunk.objects(id=chunk_id, company=company, task="").first()
    if not chunk or chunk.user != user:
        raise errors.bad_request.InvalidId("upload not found", id=chunk_id)
    if chunk.state != DatachunkState.stored:
        raise errors.bad_request.ValidationError("the upload is not stored yet", id=chunk_id)
    return chunk


def run_import(company: str, identity: Identity, chunk_id: str) -> dict:
    """Imports the staged ZIP as a new task. Returns the task id and what was attached."""
    chunk = _staged_for(company, identity.user, chunk_id)
    client = datachunks._internal()
    warnings: List[str] = []

    with tempfile.TemporaryFile() as fh:
        body = client.get_object(Bucket=chunk.bucket, Key=chunk.key)["Body"]
        for block in body.iter_chunks(chunk_size=COPY_BLOCK):
            fh.write(block)
        fh.seek(0)
        try:
            archive = zipfile.ZipFile(fh)
        except zipfile.BadZipFile:
            raise errors.bad_request.ValidationError("the upload is not a ZIP file", id=chunk_id) from None

        with archive:
            names = archive.namelist()
            root = package_root(names)
            members = set(names)
            manifest_member = f"{root}/{MANIFEST_NAME}"
            if manifest_member not in members:
                raise errors.bad_request.ValidationError("the ZIP has no manifest", field=MANIFEST_NAME)
            manifest = parse_manifest(archive.read(manifest_member).decode("utf-8", errors="replace"))

            project = _project_for(company, manifest.project)
            ProjectAccessBLL.assert_can(identity, project.id, "write", object_owner=identity.user)

            hyperparams = {}
            if f"{root}/{ARGS_NAME}" in members:
                hyperparams = hyperparams_from_args(
                    read_args(archive.read(f"{root}/{ARGS_NAME}").decode("utf-8", errors="replace"))
                )
            scalars: List[dict] = []
            if f"{root}/{SCALARS_NAME}" in members:
                scalars = read_scalars(archive.read(f"{root}/{SCALARS_NAME}").decode("utf-8", errors="replace"))
            else:
                warnings.append(f"no {SCALARS_NAME}: no scalars were imported")

            task_id = create_id()
            files = planned_artifacts(root, names)
            stored: List[Datachunk] = []
            try:
                artifacts = {}
                now = datetime.now(timezone.utc)
                for planned in files:
                    size, sha256 = _copy_and_hash(archive, planned.member)
                    key = datachunks.build_key(project.id, task_id, planned.name)
                    with archive.open(planned.member) as stream:
                        client.upload_fileobj(stream, Bucket=DatachunkKind.artifact.value, Key=key)
                    record = Datachunk(
                        id=create_id(),
                        company=company,
                        user=identity.user,
                        task=task_id,
                        project=project.id,
                        kind=DatachunkKind.artifact,
                        bucket=DatachunkKind.artifact.value,
                        key=key,
                        name=planned.name,
                        size=size,
                        sha256=sha256,
                        state=DatachunkState.stored,
                        created=now,
                        stored=now,
                    )
                    record.save()
                    stored.append(record)
                    artifact = {
                        "key": planned.name,
                        "type": planned.artifact_type,
                        "mode": "output",
                        "uri": f"{datachunks.LOCATION_PREFIX}artifact/{task_id}/{quote(planned.name, safe='/')}",
                        "hash": sha256,
                        "content_size": size,
                        "timestamp": int(now.timestamp() * 1000),
                    }
                    artifacts[get_artifact_id(artifact)] = Artifact(**artifact)

                task = Task(
                    id=task_id,
                    user=identity.user,
                    company=company,
                    created=now,
                    last_update=now,
                    last_change=now,
                    last_changed_by=identity.user,
                    name=manifest.task,
                    comment=manifest.comment,
                    type=TaskType.training,
                    project=project.id,
                    tags=manifest.tags,
                    system_tags=[],
                    hyperparams=hyperparams,
                    execution=Execution(artifacts=artifacts),
                    status=TaskStatus.created,
                )
                task.save()
                try:
                    _write_scalars(company, identity, task_id, scalars)
                    task.status = TaskStatus.completed
                    task.completed = now
                    task.save()
                except Exception:
                    task.delete()
                    event_bll.delete_task_events(company, task_id, wait_for_delete=True)
                    raise
            except Exception:
                _remove_stored(client, stored)
                raise

            if manifest.tags:
                org_bll.update_tags(
                    company, Tags.Task, projects=[project.id], tags=manifest.tags, system_tags=[]
                )
            update_project_time(project.id)

    datachunks.delete(company=company, chunks=[chunk])
    return {
        "id": task_id,
        "project": project.id,
        "name": manifest.task,
        "scalars": len(scalars),
        "artifacts": [planned.name for planned in files],
        "warnings": warnings,
    }


def _write_scalars(company: str, identity: Identity, task_id: str, scalars: Sequence[dict]) -> None:
    if not scalars:
        return
    base = es_factory.get_timestamp_millis()
    events = [
        {
            "type": "training_stats_scalar",
            "task": task_id,
            "iter": row["iteration"],
            "metric": row["metric"],
            "variant": row["variant"],
            "value": row["value"],
            "timestamp": base + index,
            "allow_locked": True,
        }
        for index, row in enumerate(scalars)
    ]
    added, errors_count, errors_info = event_bll.add_events(
        company_id=company, identity=identity, events=events, worker="import"
    )
    if errors_count or added != len(events):
        raise errors.bad_request.ValidationError(
            "scalars could not be written", added=added, expected=len(events), errors=errors_info
        )


def _remove_stored(client, stored: Sequence[Datachunk]) -> None:
    for record in stored:
        try:
            client.delete_object(Bucket=record.bucket, Key=record.key)
        except ClientError:
            pass
        record.delete()


def purge_unimported(older_than: timedelta = STAGING_MAX_AGE) -> int:
    """Removes staging uploads that were stored but never imported."""
    cutoff = datetime.utcnow() - older_than
    removed = 0
    for chunk in Datachunk.objects(task="", state=DatachunkState.stored, created__lt=cutoff):
        datachunks.delete(company=chunk.company, chunks=[chunk])
        removed += 1
    return removed
