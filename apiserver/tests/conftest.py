"""The upstream API tests under tests/automated talk to a running server, so they count as integration tests.

Tests marked needs_redis use the Redis of the configuration. Without a reachable Redis they are skipped, so a clean
checkout runs the rest of the unit tests with no services at all.

The server reads its signing secrets at import time. The tests take them from the configuration (CAIRNML_ALLOW_CONFIG_SECRET)
and get a folder with the other secrets when CAIRNML_SECRETS_DIR is not set. Both are set here, before any module of
the server is imported.
"""

import atexit
import os
import shutil
import tempfile

os.environ.setdefault("CAIRNML_ALLOW_CONFIG_SECRET", "1")
if "CAIRNML_SECRETS_DIR" not in os.environ:
    _secrets_dir = tempfile.mkdtemp(prefix="cairnml-test-secrets-")
    with open(os.path.join(_secrets_dir, "s3_secret_key"), "w", encoding="utf-8") as _handle:
        _handle.write("test")
    os.environ["CAIRNML_SECRETS_DIR"] = _secrets_dir
    atexit.register(shutil.rmtree, _secrets_dir, True)

import pytest  # noqa: E402

from apiserver.redis_manager import redman  # noqa: E402


def _redis_is_up() -> bool:
    try:
        return bool(redman.connection("apiserver").ping())
    except Exception:
        return False


def pytest_collection_modifyitems(items):
    redis_up = None
    for item in items:
        if "/tests/automated/" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
        if item.get_closest_marker("needs_redis"):
            if redis_up is None:
                redis_up = _redis_is_up()
            if not redis_up:
                item.add_marker(
                    pytest.mark.skip(reason="needs a Redis server (CAIRNML_REDIS_SERVICE_HOST and _PORT, default localhost:6379)")
                )
