"""Migration 2_5_2 drops the collections of the removed models and keeps them when the keep switch is set."""

import importlib

import pytest
from mongoengine import get_db

from apiserver.database import Database

from apiserver.tests.db import ensure_db

ensure_db()
migration = importlib.import_module("apiserver.mongo.migrations.2_5_2")


@pytest.fixture
def backend():
    database = get_db(Database.backend)
    for name in migration.REMOVED_COLLECTIONS:
        database[name].insert_one({"seeded": True})
    yield database
    for name in migration.REMOVED_COLLECTIONS:
        database.drop_collection(name)


def test_removed_collections_are_dropped(backend, monkeypatch):
    monkeypatch.delenv(migration.KEEP_ENV, raising=False)
    migration.migrate_backend(backend)
    assert not set(migration.REMOVED_COLLECTIONS) & set(backend.list_collection_names())


def test_removed_collections_are_kept_with_the_switch(backend, monkeypatch):
    monkeypatch.setenv(migration.KEEP_ENV, "1")
    migration.migrate_backend(backend)
    assert set(migration.REMOVED_COLLECTIONS) <= set(backend.list_collection_names())
