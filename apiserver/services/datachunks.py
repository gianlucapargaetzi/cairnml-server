"""Object storage endpoints. The bytes never pass through this server: it signs the URL, then checks the commit."""

from apiserver.apierrors import errors
from apiserver.apimodels.datachunks import (
    CommitRequest,
    DeleteRequest,
    PrepareDownloadRequest,
    PreparePartRequest,
    PrepareUploadRequest,
)
from apiserver.bll import datachunks as bll
from apiserver.bll.task.utils import get_task_with_write_access
from apiserver.database.model.datachunk import Datachunk
from apiserver.database.model.task.task import Task
from apiserver.idp.tokens import read_claims
from apiserver.service_repo import APICall, endpoint


def _job_task(call: APICall):
    """The task named by the caller's job token, or None for any other caller."""
    header = call.authorization or ""
    if not header.lower().startswith("bearer "):
        return None
    claims = read_claims(header[7:].strip())
    return claims.get("task") if claims.get("kind") == "job" else None


def _require_task_access(call: APICall, company: str, task_id: str) -> None:
    """A job token acts only for its own task; anyone else needs write access to the task."""
    job_task = _job_task(call)
    if job_task is not None:
        if job_task != task_id:
            raise errors.forbidden.NoWritePermission("a job token only acts for its own task")
        return
    get_task_with_write_access(task_id=task_id, company_id=company, identity=call.identity, only=("id",))


@endpoint("datachunks.prepare_upload")
def prepare_upload(call: APICall, company: str, request: PrepareUploadRequest):
    _require_task_access(call, company, request.task)
    task = Task.objects(id=request.task, company=company).only("id", "project", "system_tags").first()
    call.result.data = bll.prepare_upload(
        company=company,
        task_id=request.task,
        project=task.project,
        task_tags=task.system_tags,
        kind=request.kind,
        name=request.name,
        size=request.size,
        sha256=request.sha256,
        content_type=request.content_type,
    )


@endpoint("datachunks.commit")
def commit(call: APICall, company: str, request: CommitRequest):
    chunk = Datachunk.objects(id=request.id, company=company).only("task").first()
    if not chunk:
        raise errors.bad_request.InvalidId("datachunk not found", id=request.id)
    _require_task_access(call, company, chunk.task)
    stored = bll.commit(company=company, chunk_id=request.id, parts=request.parts)
    call.result.data = {"id": stored.id, "state": stored.state}


@endpoint("datachunks.prepare_part")
def prepare_part(call: APICall, company: str, request: PreparePartRequest):
    chunk = Datachunk.objects(id=request.id, company=company).only("task").first()
    if not chunk:
        raise errors.bad_request.InvalidId("datachunk not found", id=request.id)
    _require_task_access(call, company, chunk.task)
    call.result.data = bll.prepare_part(company=company, chunk_id=request.id, part=request.part)


def _by_id_or_name(company: str, request) -> Datachunk:
    if request.id:
        chunk = Datachunk.objects(id=request.id, company=company).first()
        if not chunk:
            raise errors.bad_request.InvalidId("datachunk not found", id=request.id)
        return chunk
    if request.task and request.kind and request.name:
        return bll.find_stored(company=company, task_id=request.task, kind=request.kind, name=request.name)
    raise errors.bad_request.MissingRequiredFields("give an id, or task, kind and name")


@endpoint("datachunks.prepare_download")
def prepare_download(call: APICall, company: str, request: PrepareDownloadRequest):
    chunk = _by_id_or_name(company, request)
    bll.check_read_access(company=company, task_id=chunk.task, job_task=_job_task(call))
    call.result.data = bll.prepare_download(company=company, chunk=chunk)


@endpoint("datachunks.delete")
def delete(call: APICall, company: str, request: DeleteRequest):
    if request.id:
        chunks = list(Datachunk.objects(id=request.id, company=company))
        if not chunks:
            raise errors.bad_request.InvalidId("datachunk not found", id=request.id)
    elif request.task and request.kind and request.name:
        chunks = list(Datachunk.objects(company=company, task=request.task, kind=request.kind, name=request.name))
    else:
        raise errors.bad_request.MissingRequiredFields("give an id, or task, kind and name")
    for task_id in {chunk.task for chunk in chunks}:
        _require_task_access(call, company, task_id)
    call.result.data = {"deleted": bll.delete(company=company, chunks=chunks)}
