"""Importing the server's modules must not need Redis: the unit tests import them with no services running."""

from pathlib import Path

from pyhocon import ConfigFactory

from apiserver.redis_manager import RedisManager
from apiserver.schema import SchemaReader


def test_getting_a_redis_connection_sends_no_command():
    manager = RedisManager(ConfigFactory.from_dict({"apiserver": {"host": "127.0.0.1", "port": 6379, "db": 0}}))
    client = manager.aliases["apiserver"]

    def no_round_trip(*args, **kwargs):
        raise AssertionError(f"a command was sent to Redis: {args}")

    original = client.execute_command
    client.execute_command = no_round_trip
    try:
        assert manager.connection("apiserver") is client
    finally:
        client.execute_command = original


def test_the_schema_is_read_without_redis(tmp_path: Path):
    cache = tmp_path / "_cache.json"
    reader = SchemaReader()
    reader.cache_path = cache
    schema = reader.get_schema()

    assert cache.exists()
    assert schema is not None
    assert (tmp_path / "_cache.lock").exists()
