"""Shared helper for the project-access tests that save real documents (Task 2 and later in the
2026-10-24 project-access-control plan). Call ensure_db() once per test module, before any
Project/Task/Model is saved. It is safe to call from every module that needs it: the actual
mongoengine connection is only registered once per process.

Needs a real MongoDB reachable at the default config (mongodb://127.0.0.1:27017), which the CI
workflow and a local development environment both provide — see .github/workflows/ci.yml.
"""
from apiserver import database as db

_initialized = False


def ensure_db() -> None:
    global _initialized
    if not _initialized:
        db.db.initialize()
        _initialized = True
