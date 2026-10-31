"""Import of a run from a standard ZIP: stage the ZIP, then import it as a new task. See bll/run_import.py."""

from apiserver.apierrors import errors
from apiserver.apimodels.imports import CommitRequest, PreparePartRequest, PrepareUploadRequest, RunRequest
from apiserver.bll import datachunks
from apiserver.bll import run_import
from apiserver.database.model.datachunk import Datachunk
from apiserver.service_repo import APICall, endpoint


def _own_staged_upload(call: APICall, company: str, chunk_id: str) -> Datachunk:
    """A staging upload is visible only to the user who asked for it. Any other id gets the same answer as a
    missing one."""
    chunk = Datachunk.objects(id=chunk_id, company=company, task="").first()
    if not chunk or chunk.user != call.identity.user:
        raise errors.bad_request.InvalidId("upload not found", id=chunk_id)
    return chunk


@endpoint("imports.prepare_upload")
def prepare_upload(call: APICall, company: str, request: PrepareUploadRequest):
    call.result.data = datachunks.prepare_upload(
        company=company,
        task_id="",
        project="",
        task_tags=[],
        kind="artifact",
        name=request.name,
        size=request.size,
        sha256=request.sha256,
        content_type=request.content_type,
        user=call.identity.user,
        staging=True,
    )


@endpoint("imports.prepare_part")
def prepare_part(call: APICall, company: str, request: PreparePartRequest):
    _own_staged_upload(call, company, request.id)
    call.result.data = datachunks.prepare_part(company=company, chunk_id=request.id, part=request.part)


@endpoint("imports.commit")
def commit(call: APICall, company: str, request: CommitRequest):
    _own_staged_upload(call, company, request.id)
    stored = datachunks.commit(company=company, chunk_id=request.id, parts=request.parts)
    call.result.data = {"id": stored.id, "state": stored.state}


@endpoint("imports.run")
def run(call: APICall, company: str, request: RunRequest):
    _own_staged_upload(call, company, request.id)
    call.result.data = run_import.run_import(company, call.identity, request.id)
