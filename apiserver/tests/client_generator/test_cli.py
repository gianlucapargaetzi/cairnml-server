import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "schema"


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "apiserver.client_generator", *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[3],
    )


@pytest.fixture
def schema(tmp_path):
    # a schema folder with a `queues` service, so the real target table can be used
    folder = tmp_path / "schema"
    folder.mkdir()
    shutil.copy(FIXTURES / "demo.conf", folder / "queues.conf")
    return folder


def _generate(schema, out, *extra):
    return _run(
        "generate", "--target", "sdk", "--services", "queues", "--schema", str(schema), "--out", str(out), *extra
    )


def test_check_fails_naming_a_stale_file(schema, tmp_path):
    out = tmp_path / "out"
    assert _generate(schema, out).returncode == 0
    target = out / "queues.py"
    assert target.is_file()
    target.write_text(target.read_text().replace("Gets an entry", "Gets an entrz"))
    result = _generate(schema, out, "--check")
    assert result.returncode == 1
    assert "queues.py is out of date" in result.stdout + result.stderr


def test_check_passes_on_fresh_output(schema, tmp_path):
    out = tmp_path / "out"
    assert _generate(schema, out).returncode == 0
    assert _generate(schema, out, "--check").returncode == 0


def test_check_fails_for_a_missing_file(schema, tmp_path):
    result = _generate(schema, tmp_path / "empty", "--check")
    assert result.returncode == 1
    assert "queues.py is missing" in result.stdout + result.stderr


def test_unknown_service_or_target_is_a_usage_error(schema, tmp_path):
    out = tmp_path / "out"
    bad_service = _run("generate", "--target", "sdk", "--services", "nope", "--schema", str(schema), "--out", str(out))
    assert bad_service.returncode == 2
    bad_target = _run("generate", "--target", "nope", "--schema", str(schema), "--out", str(out))
    assert bad_target.returncode == 2


CALLERS = Path(__file__).parent / "fixtures" / "callers"


def _check_callers(schema, root, *extra):
    return _run("check-callers", "--target", "sdk", "--schema", str(schema), "--root", str(root), *extra)


def test_check_callers_exit_codes(schema, tmp_path):
    # the fixture schema is installed as `queues`, the one service of the sdk target
    clean = tmp_path / "clean.py"
    clean.write_text("from backend_api.services import queues\nqueues.GetByIdRequest(queue='q')\n")
    assert _check_callers(schema, clean).returncode == 0

    bad = tmp_path / "bad.py"
    bad.write_text("from backend_api.services import queues\nqueues.CreateRequest(name='x')\n")
    result = _check_callers(schema, bad)
    assert result.returncode == 1
    assert "queues.create is not in the schema" in result.stdout

    allow = tmp_path / "allow.txt"
    allow.write_text("exempt queues.create a test exemption\n")
    assert _check_callers(schema, bad, "--allow", str(allow)).returncode == 0

    allow.write_text("exempt queues.create\n")
    assert _check_callers(schema, bad, "--allow", str(allow)).returncode == 2
    assert _check_callers(schema, tmp_path / "missing").returncode == 2
