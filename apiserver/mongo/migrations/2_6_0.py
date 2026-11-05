import uuid
from datetime import datetime

from pymongo.database import Database

RESOURCE_KEY = ("company", "resource_type", "resource_id", "project", "user")
""" The fields that make a share unique, as the unique index of the share collection has them """


def _grants(db: Database):
    """The shares the existing grants give. The old fields are kept: nothing is removed here."""
    for queue in db["queue"].find({}, {"company": 1, "created_by": 1, "public": 1, "users": 1}):
        if queue.get("public"):
            # a public queue is usable by every user; granted_by is the owner, empty for a queue without one
            yield _share(queue["company"], "queue", queue["_id"], "public", "", "", queue.get("created_by") or "")
        for grant in queue.get("users") or []:
            # a grant to one user has no project (see Share, scope user)
            yield _share(queue["company"], "queue", queue["_id"], "user", "", grant["user"], grant["added_by"])

    # queues are allocated to root projects only; sub-projects use the queues of their root (bll/queue/queue_access)
    for root in db["project"].find({"parent": None, "queues": {"$ne": []}}, {"company": 1, "user": 1, "queues": 1}):
        owner = root.get("user")
        if not owner:
            # nobody but an admin could see or change the allocations of an ownerless project: no access to keep
            continue
        for queue_id in root["queues"]:
            yield _share(root["company"], "queue", queue_id, "projects", root["_id"], "", owner)

    for access in db["user_access"].find({}, {"company": 1, "user": 1, "granted_projects": 1}):
        for root_id in access.get("granted_projects") or []:
            yield _share(access["company"], "credential", access["_id"], "projects", root_id, "", access["user"])

    for dataset in db["dataset"].find({}, {"company": 1, "kind": 1, "owner": 1, "visibility": 1, "projects": 1}):
        resource_type = "registry" if dataset.get("kind") == "registry" else "dataset"
        if dataset.get("visibility") == "Public":
            yield _share(dataset["company"], resource_type, dataset["_id"], "public", "", "", dataset["owner"])
        for root_id in dataset.get("projects") or []:
            yield _share(dataset["company"], resource_type, dataset["_id"], "projects", root_id, "", dataset["owner"])


def _share(company: str, resource_type: str, resource_id: str, scope: str, project: str, user: str,
           granted_by: str) -> dict:
    return {
        "company": company, "resource_type": resource_type, "resource_id": resource_id, "scope": scope,
        "project": project, "user": user, "level": "read", "granted_by": granted_by,
    }


def migrate_backend(db: Database):
    """
    Every grant the server had is written as a read share, so the access checks of the share record (bll/access) keep
    the same users: a public queue or dataset, a queue granted to one user, a queue allocated to a root project, a
    credential shared with a root project, a dataset allocated to a root project, and a registry, public or allocated.

    A project share is recorded as granted by the owner of the root project (for a queue, the owner allocates it) or by
    the owner of the dataset or credential. A share that already exists is left as it is, so the migration may run
    again.
    """
    shares = db["share"]
    now = datetime.utcnow()
    for grant in _grants(db):
        key = {field: grant[field] for field in RESOURCE_KEY}
        if shares.find_one(key, {"_id": 1}) is not None:
            continue
        shares.insert_one({
            "_id": uuid.uuid4().hex, **grant, "inherited": False, "created": now,
        })
