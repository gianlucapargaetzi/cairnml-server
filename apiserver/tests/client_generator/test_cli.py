import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apiserver.client_generator.python_renderer import render_package
from apiserver.client_generator.schema_model import load_schema

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


def test_the_whole_target_writes_the_package_file_naming_every_service(schema, tmp_path):
    out = tmp_path / "out"
    result = _run("generate", "--target", "sdk", "--schema", str(schema), "--out", str(out))
    assert result.returncode == 0, result.stderr
    assert (out / "__init__.py").read_text() == render_package(["queues"], load_schema(schema))


def test_a_subset_of_the_services_leaves_the_package_file_alone(tmp_path):
    folder = tmp_path / "schema"
    folder.mkdir()
    shutil.copy(FIXTURES / "demo.conf", folder / "queues.conf")
    shutil.copy(FIXTURES / "enums.conf", folder / "tasks.conf")
    out = tmp_path / "out"
    result = _run("generate", "--target", "sdk", "--services", "tasks", "--schema", str(folder), "--out", str(out))
    assert result.returncode == 0, result.stderr
    assert (out / "tasks.py").is_file()
    assert not (out / "__init__.py").exists()


def test_check_fails_naming_a_stale_package_file(schema, tmp_path):
    out = tmp_path / "out"
    assert _run("generate", "--target", "sdk", "--schema", str(schema), "--out", str(out)).returncode == 0
    (out / "__init__.py").write_text("__all__ = []\n")
    result = _run("generate", "--target", "sdk", "--schema", str(schema), "--out", str(out), "--check")
    assert result.returncode == 1
    assert "__init__.py is out of date" in result.stdout + result.stderr


def test_check_fails_naming_a_missing_package_file(schema, tmp_path):
    out = tmp_path / "out"
    generate = ("generate", "--target", "sdk", "--services", "queues", "--schema", str(schema), "--out", str(out))
    assert _run(*generate).returncode == 0
    (out / "__init__.py").unlink()
    result = _run("generate", "--target", "sdk", "--schema", str(schema), "--out", str(out), "--check")
    assert result.returncode == 1
    assert "__init__.py is missing" in result.stdout + result.stderr


def test_the_agent_target_is_checked_but_never_generated(schema, tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_text("from backend_api.services import queues\nqueues.GetByIdRequest(queue='q')\n")
    assert _run("check-callers", "--target", "agent", "--schema", str(schema), "--root", str(clean)).returncode == 0
    generated = _run("generate", "--target", "agent", "--schema", str(schema), "--out", str(tmp_path / "out"))
    assert generated.returncode == 2
    assert "only checked" in generated.stderr


def test_check_callers_judges_session_calls_of_a_target_that_checks_them(schema, tmp_path):
    caller = tmp_path / "caller.py"
    caller.write_text(
        "class Session:\n    def send_request(self):\n        pass\n\n"
        "def run(session):\n    session.send_request()\n    session.finalize_config()\n"
    )
    result = _run("check-callers", "--target", "sdk", "--schema", str(schema), "--root", str(caller))
    assert result.returncode == 1
    assert "session.finalize_config is not a method of the Session class" in result.stdout
    assert "send_request" not in result.stdout


def test_check_callers_reports_a_call_by_name_with_its_request_class(schema, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text('def run(session):\n    session.send_request(service="queues", action="get_by_id", queue="q")\n')
    result = _check_callers(schema, bad)
    assert result.returncode == 1
    assert (
        "send_request names queues.get_by_id by strings: build queues.GetByIdRequest and send it instead"
        in result.stdout
    )


def test_check_callers_reads_a_base_folder_for_the_classes_a_session_inherits_from(schema):
    alias = Path(__file__).parent / "fixtures" / "callers" / "alias"
    root = str(alias / "agent_pkg")
    without = _check_callers(schema, alias / "agent_pkg")
    assert "Session inherits from SdkSession" in without.stdout
    assert "session.of is not a method" not in without.stdout
    with_base = _run(
        "check-callers", "--target", "sdk", "--schema", str(schema), "--root", root, "--base", str(alias / "cairnml")
    )
    assert with_base.returncode == 0, with_base.stdout + with_base.stderr
    missing = _run(
        "check-callers", "--target", "sdk", "--schema", str(schema), "--root", root, "--base", str(alias / "missing")
    )
    assert missing.returncode == 2
