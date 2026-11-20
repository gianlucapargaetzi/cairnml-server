"""Finds the records whose plain-string reference names a record that no longer exists (an orphan).

The database does not enforce these references, so each delete path cleans up by hand. The tests use find_orphans after
every delete path; nothing here changes data.
"""

from typing import Dict, List

from apiserver.database.model.auth import AuthUser
from apiserver.database.model.datachunk import Datachunk
from apiserver.database.model.device import Device
from apiserver.database.model.project import Project
from apiserver.database.model.repository import Assignment, Repository, RepositoryCheck
from apiserver.database.model.share import Share
from apiserver.database.model.task.task import Task
from apiserver.database.model.user_access import UserAccess

# (kind, referencing model, field, referenced model[, filter]). An empty value means "no reference" and is not checked.
# The optional filter is a query on the referencing records: only those that match are checked. A revoked device is kept
# as audit history when its owner is deleted, so it may name a user who is gone; a live device may not.
REFERENCES = (
    ("assignment.repository", Assignment, "repository", Repository),
    ("assignment.credential", Assignment, "credential", UserAccess),
    ("assignment.user", Assignment, "user", AuthUser),
    ("share.project", Share, "project", Project),
    ("share.user", Share, "user", AuthUser),
    ("user_access.user", UserAccess, "user", AuthUser),
    ("repository_check.user", RepositoryCheck, "user", AuthUser),
    ("device.user", Device, "user", AuthUser, {"revoked": False}),
    ("datachunk.task", Datachunk, "task", Task),  # a staged datachunk has no task and is not an orphan
    ("datachunk.project", Datachunk, "project", Project),
)


def find_orphans(company: str) -> Dict[str, List[str]]:
    """The ids of the referencing records of the company whose referenced record is missing, per kind.
    Kinds with no orphan are left out, so an empty dict means no orphan at all."""
    found = {}
    for kind, model, field, target, *only_where in REFERENCES:
        query = only_where[0] if only_where else {}
        records = model.objects(company=company, **{f"{field}__nin": ["", None]}, **query).only("id", field)
        values = {getattr(record, field) for record in records}
        if not values:
            continue
        existing = set(target.objects(id__in=list(values)).scalar("id"))
        orphans = [record.id for record in records if getattr(record, field) not in existing]
        if orphans:
            found[kind] = orphans
    return found
