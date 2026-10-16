from pymongo.database import Database


def migrate_backend(db: Database):
    """
    Queues are removed: the tasks no longer name a queue, and the queue collection is dropped.
    Pending tasks are the tasks in the queued status; the scheduler places them.
    """
    for collection in ("task", "task__trash"):
        if collection in db.list_collection_names():
            db[collection].update_many({"execution.queue": {"$exists": True}}, {"$unset": {"execution.queue": ""}})
    if "queue" in db.list_collection_names():
        db.drop_collection("queue")
