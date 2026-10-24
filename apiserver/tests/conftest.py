"""The upstream API tests under tests/automated talk to a running server, so they count as integration tests."""

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "/tests/automated/" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
