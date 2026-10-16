from pymongo.database import Database, Collection


def _get_ids():
    # Fixed users (username and password) are removed. The server refuses to start with them
    # enabled, so there is never a mapping to apply and this migration does nothing.
    return None


def _switch_uuid(collection: Collection, uuid_field: str, uuids: dict):
    docs = list(collection.find({uuid_field: {"$in": [uuids]}}))
    if not docs:
        return
    replaced_uuids = [doc[uuid_field] for doc in docs]
    for doc in docs:
        doc[uuid_field] = uuids[doc[uuid_field]]
    collection.insert_many(docs)
    collection.delete_many({uuid_field: {"$in": replaced_uuids}})


def migrate_auth(db: Database):
    uuids = _get_ids()
    if not uuids:
        return

    collection: Collection = db["user"]
    collection.drop_indexes()

    _switch_uuid(collection=collection, uuid_field="_id", uuids=uuids)


def migrate_backend(db: Database):
    uuids = _get_ids()
    if not uuids:
        return

    for name in ("project", "task", "model"):
        _switch_uuid(collection=db[name], uuid_field="user", uuids=uuids)
