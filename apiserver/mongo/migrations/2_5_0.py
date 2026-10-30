from pymongo.database import Database


def migrate_auth(db: Database):
    """
    Audit events are kept for 365 days: a TTL index on ts removes the older ones. The index was a plain one on ts; it
    is replaced here, since MongoDB does not change the options of an existing index. The model creates the same
    index on a new database.
    """
    collection = db["audit_event"]
    for name, info in collection.index_information().items():
        if info.get("key") == [("ts", 1)]:
            collection.drop_index(name)
    collection.create_index([("ts", 1)], name="ts_1", expireAfterSeconds=365 * 24 * 60 * 60)
