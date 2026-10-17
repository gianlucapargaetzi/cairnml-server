"""Object storage endpoints. The bytes never pass through this server: it signs the URL, then checks the commit."""

from apiserver.apierrors import errors
from apiserver.apimodels.datachunks import CommitRequest, PrepareDownloadRequest, PrepareUploadRequest
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


@endpoint("datachunks.prepare_upload")
def prepare_upload(call: APICall, company: str, request: PrepareUploadRequest):
    job_task = _job_task(call)
    if job_task is not None and job_task != request.task:
        raise errors.forbidden.NoWritePermission("a job token only uploads for its own task")

    task = get_task_with_write_access(
        task_id=request.task,
        company_id=company,
        identity=call.identity,
        only=("id", "project", "system_tags"),
    )
    call.result.data = bll.prepare_upload(
        company=company,
        task_id=task.id,
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
    job_task = _job_task(call)
    if job_task is not None and job_task != chunk.task:
        raise errors.forbidden.NoWritePermission("a job token only commits its own task's uploads")
    if job_task is None:
        get_task_with_write_access(
            task_id=chunk.task, company_id=company, identity=call.identity, only=("id",)
        )

    stored = bll.commit(company=company, chunk_id=request.id)
    call.result.data = {"id": stored.id, "state": stored.state}


@endpoint("datachunks.prepare_download")
def prepare_download(call: APICall, company: str, request: PrepareDownloadRequest):
    call.result.data = bll.prepare_download(company=company, chunk_id=request.id)
