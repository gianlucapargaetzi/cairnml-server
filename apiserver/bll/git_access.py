"""Which Git credential a job may read its repository with, and the token it gets: the one place that decides it, and
the one place that audits the answer.

A hosted repository (one of the repositories the server records: GitHub or another host) is read as bll/repositories says:
the project owner's assignment, else the user's own, else without a credential when the host allows it, else the company's
access. Any other address is read with the Git credentials stored for its host (and path prefix). A refusal says why, in
the words the caller shows; the audit records of git_token (issued, public, refused) are unchanged.
"""

from dataclasses import dataclass
from typing import Any, Optional, Union

from apiserver.apierrors import errors
from apiserver.bll import repositories as repositories_bll
from apiserver.bll.dataset_layout import verify_dataset
from apiserver.idp import git_credentials as store
from apiserver.idp.audit import record as audit
from apiserver.idp.git_reader import GitReader

STORED = "stored"  # the address is not a hosted repository: a Git credential stored for its host is used
HOSTED = "hosted"  # a hosted repository, read with an access record, a fallback or no credential
DATASET_NOT_HOSTED = "dataset-not-hosted"


@dataclass(frozen=True)
class Granted:
    answer: dict  # {"token", "expires_at", "username"}
    path: str  # STORED or HOSTED
    host: str = ""  # STORED: the stored credential's host and path
    stored_path: str = ""
    repo: Any = None  # HOSTED: the repository record
    entry_id: str = ""  # HOSTED: the access record that was used, "" when none
    entry_kind: str = ""  # HOSTED: "app" or "token", "" when none
    warning: str = ""  # HOSTED: why a fallback was used, "" when none


@dataclass(frozen=True)
class Refused:
    reason: str
    path: str
    repo: Any = None


Decision = Union[Granted, Refused]


def _stored(company: str, repository: str) -> Decision:
    host, _path = store.host_and_path(repository)
    entry = store.find_credential(company, repository)
    if entry is None:
        return Refused(reason="no Git credentials stored", path=STORED)
    return Granted(
        answer={"token": store.secret_of(entry), "expires_at": None, "username": entry.username},
        path=STORED,
        host=entry.host,
        stored_path=entry.path,
    )


def _hosted(company: str, repo, project: Optional[str], read_user: str) -> Decision:
    try:
        answer, resolution = repositories_bll.token_for_job(company, repo, project, read_user)
    except errors.bad_request.ValidationError as refused:
        return Refused(reason=str(refused), path=HOSTED, repo=repo)
    entry = resolution.entry
    return Granted(
        answer=answer,
        path=HOSTED,
        repo=repo,
        entry_id=entry.id if entry is not None else "",
        entry_kind=entry.kind if entry is not None else "",
        warning=resolution.warning,
    )


def access_for(
    company: str, repository: str, *, record_user: str, read_user: str, project: Optional[str], dataset: bool = False
) -> Decision:
    """The decision for a repository a task names. ``record_user`` is who the record is made for (the task's user),
    ``read_user`` whose access reads it (the one who queued the task, when there is one). ``dataset`` is set when the
    request names the repository itself (a dataset on a registry), not the task's code."""
    repo = repositories_bll.record_use_quietly(
        company, repository, "dataset" if dataset else "code", record_user, project
    )
    if repo is None:
        if dataset:
            return Refused(reason="the repository is not a hosted git repository", path=DATASET_NOT_HOSTED)
        return _stored(company, repository)
    return _hosted(company, repo, project, read_user)


def issue_for_task(
    company: str,
    actor: str,
    device: Optional[str],
    task,
    task_id: str,
    requested_repository: Optional[str],
    ip: Optional[str],
) -> dict:
    """The git_token answer for a task: decided by access_for, audited as the git_token endpoint always was (issued, public,
    refused). Raises a validation error when the access is refused."""
    repository = requested_repository or (task.script.repository if task.script else None)
    if not repository:
        raise errors.bad_request.ValidationError("the task has no repository")
    read_user = (getattr(getattr(task, "execution", None), "enqueued_by", None)) or task.user
    decision = access_for(
        company,
        repository,
        record_user=task.user,
        read_user=read_user,
        project=task.project,
        dataset=bool(requested_repository),
    )
    if isinstance(decision, Refused):
        if decision.path == DATASET_NOT_HOSTED:
            raise errors.bad_request.ValidationError(decision.reason)
        if decision.path == STORED:
            host, _path = store.host_and_path(repository)
            audit(actor, "git_token.refused", ip=ip, company=company, task=task_id, host=host, reason=decision.reason)
            raise errors.bad_request.ValidationError(
                f"no Git credentials are stored for {host}: an admin adds them with the git_credentials.set call"
            )
        audit(actor, "git_token.refused", ip=ip, company=company, device=device, task=task_id, reason=decision.reason)
        raise errors.bad_request.ValidationError(decision.reason)
    if decision.path == STORED:
        audit(
            actor,
            "git_token.issued",
            ip=ip,
            company=company,
            task=task_id,
            host=decision.host,
            path=decision.stored_path,
        )
        return decision.answer
    repo, answer = decision.repo, decision.answer
    if requested_repository and "registry" not in repo.roles and decision.entry_kind == "app":
        try:
            with GitReader(repo.host, answer.get("token"), answer.get("username")) as reader:
                verify_dataset(reader, repo.path)
        except errors.bad_request.ValidationError as refused:
            audit(actor, "git_token.refused", ip=ip, company=company, device=device, task=task_id, reason=str(refused))
            raise
    audit(
        actor,
        "git_token.issued" if answer["token"] else "git_token.public",
        ip=ip,
        company=company,
        device=device,
        task=task_id,
        repo=f"{repo.host}/{repo.path}",
        via="assignment",
        access=decision.entry_id,
        fallback=decision.warning,
    )
    return answer
