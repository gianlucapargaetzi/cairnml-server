"""Drops the collections of the models that were removed from the server.

Their documents are not read by any code any more. Set CAIRNML_KEEP_REMOVED_COLLECTIONS=1 to keep them; their document
counts are logged either way.
"""

import logging as log
import os

from pymongo.database import Database

REMOVED_COLLECTIONS = (
    "repository_source",  # CodeRepository
    "repository_binding",  # RepositoryBinding
    "dataset",  # Dataset
    "dataset_registry",  # DatasetRegistry
    "scheduler_workers",  # SchedulerWorker
    "storage_settings",  # StorageSettings
)
KEEP_ENV = "CAIRNML_KEEP_REMOVED_COLLECTIONS"


def migrate_backend(db: Database):
    keep = os.environ.get(KEEP_ENV) == "1"
    existing = set(db.list_collection_names())
    for name in REMOVED_COLLECTIONS:
        if name not in existing:
            continue
        count = db[name].estimated_document_count()
        if keep:
            log.warning(f"Keeping removed collection {name} ({count} documents) because {KEEP_ENV}=1")
            continue
        log.warning(f"Dropping removed collection {name} ({count} documents)")
        db.drop_collection(name)
