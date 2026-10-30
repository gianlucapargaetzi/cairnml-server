from pymongo.database import Database


def migrate_backend(db: Database):
    """
    Queues are allocated to projects and granted to users (see QueueAccessBLL). The default queue is public, so every
    user can use it; the other queues start without grants and not public, which is the default of the field.
    """
    db["queue"].update_many({"system_tags": "default"}, {"$set": {"public": True}})
