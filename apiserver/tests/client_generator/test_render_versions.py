"""The retained definitions of an action: one module per version for each older definition, and the checks of the
command line that keep those modules in step with the schema."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apiserver.client_generator.python_renderer import Unsupported, render_service_files
from apiserver.client_generator.schema_model import load_schema

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "schema"
SESSION = "cairnml.backend_api.session"


def _files():
    return render_service_files(load_schema(FIXTURES)["demo"], SESSION)


def test_the_service_module_holds_the_newest_definition_of_each_action():
    files = _files()
    text = files["demo.py"]
    assert '_version = "2.30"' in text
    assert '_version = "2.1"' not in text
    assert "class GetAllRequest(Request):" in text
    assert '"Lists entries (oldest definition)"' not in text


def test_each_older_definition_is_a_module_of_the_version_at_which_it_began():
    files = _files()
    assert set(files) == {"demo.py", "legacy/v2_1/demo.py", "legacy/v2_15/demo.py"}


def test_a_legacy_module_has_the_classes_of_its_definition_under_the_same_names():
    files = _files()
    old = files["legacy/v2_1/demo.py"]
    assert "class GetAllRequest(Request):" in old
    assert "class GetAllResponse(Response):" in old
    assert '_version = "2.1"' in old
    assert '_action = "get_all"' in old
    assert "Lists entries (oldest definition)" in old
    assert "legacy definitions first served at API version 2.1" in old  # the docstring names the version
    assert "tags" not in old  # the 2.30 field is not in the 2.1 definition
    assert "page" not in old


def test_a_legacy_module_is_the_same_on_a_second_run():
    assert _files() == _files()


def test_a_legacy_module_keeps_the_response_of_its_own_version():
    text = _files()["legacy/v2_15/demo.py"]
    assert "class GetAllResponse(Response):" in text
    assert "count: Optional[int]" in text
    assert "Legacy" not in text  # the 2.1 response is not in the 2.15 module


def test_a_legacy_module_parses_as_python_3_9():
    import ast

    for text in _files().values():
        ast.parse(text, feature_version=(3, 9))


def test_a_legacy_batch_batches_the_batched_definition_of_its_version_through_a_lazy_import(tmp_path):
    folder = tmp_path / "schema"
    folder.mkdir()
    (folder / "batches.conf").write_text(
        'run {\n    "1.0" { request { type: object, properties { id { type: string } } }, response { type: object } }\n'
        '    "1.5" { request { type: object, properties { id { type: string } } }, response { type: object } }\n}\n'
        'batch {\n    "1.0" { batch_request { action: run } }\n    "1.1" { batch_request { action: run } }\n'
        '    "1.2" { batch_request { action: run } }\n}\n'
    )
    files = render_service_files(load_schema(folder)["batches"], SESSION)
    # the 1.1 batch batches run as of 1.1, which is the 1.0 definition of run (in folder v1_0, not its own folder)
    legacy = files["legacy/v1_1/batches.py"]
    assert "from ..v1_0.batches import RunRequest" in legacy
    assert "def _batched_request_cls(self):" in legacy
    # the 1.0 batch is in the same module as the 1.0 run: a plain class attribute
    assert "_batched_request_cls = RunRequest" in files["legacy/v1_0/batches.py"]
    # the newest batch batches the newest run, in the service module
    assert "_batched_request_cls = RunRequest" in files["batches.py"]


CAP_SCHEMA = (
    "items {\n"
    + "".join(f'    "1.{i}" {{ request {{ type: object }}, response {{ type: object }} }}\n' for i in range(6))
    + "}\n"
)


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "apiserver.client_generator", *args],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[3],
    )


@pytest.fixture
def schema(tmp_path):
    folder = tmp_path / "schema"
    folder.mkdir()
    shutil.copy(FIXTURES / "demo.conf", folder / "queues.conf")
    return folder


def _generate(schema, out, *extra):
    return _run("generate", "--target", "sdk", "--schema", str(schema), "--out", str(out), *extra)


def test_the_whole_target_writes_the_legacy_modules_and_their_package_files(schema, tmp_path):
    out = tmp_path / "out"
    assert _generate(schema, out).returncode == 0
    assert (out / "legacy" / "v2_1" / "queues.py").is_file()
    assert (out / "legacy" / "v2_15" / "queues.py").is_file()
    assert (out / "legacy" / "__init__.py").is_file()
    assert (out / "legacy" / "v2_15" / "__init__.py").is_file()
    assert not (out / "legacy" / "v2_30").exists()


def test_check_passes_on_fresh_output_with_legacy_modules(schema, tmp_path):
    out = tmp_path / "out"
    assert _generate(schema, out).returncode == 0
    assert _generate(schema, out, "--check").returncode == 0


def test_check_fails_naming_a_stale_legacy_module(schema, tmp_path):
    out = tmp_path / "out"
    assert _generate(schema, out).returncode == 0
    target = out / "legacy" / "v2_1" / "queues.py"
    target.write_text(target.read_text().replace("oldest definition", "oldest definitio"))
    result = _generate(schema, out, "--check")
    assert result.returncode == 1
    assert "legacy/v2_1/queues.py is out of date" in result.stdout + result.stderr


def test_check_fails_for_a_missing_legacy_module(schema, tmp_path):
    out = tmp_path / "out"
    assert _generate(schema, out).returncode == 0
    (out / "legacy" / "v2_15" / "queues.py").unlink()
    result = _generate(schema, out, "--check")
    assert result.returncode == 1
    assert "legacy/v2_15/queues.py is missing" in result.stdout + result.stderr


def test_a_legacy_module_of_a_definition_no_longer_in_the_schema_is_reported_and_removed(schema, tmp_path):
    out = tmp_path / "out"
    assert _generate(schema, out).returncode == 0
    stale = out / "legacy" / "v1_9" / "queues.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("# left over\n")
    result = _generate(schema, out, "--check")
    assert result.returncode == 1
    assert "legacy/v1_9/queues.py is not generated any more" in result.stdout + result.stderr
    assert _generate(schema, out).returncode == 0
    assert not stale.exists()
    assert not stale.parent.exists()


def test_an_action_with_more_than_five_definitions_is_refused_naming_it(tmp_path):
    folder = tmp_path / "schema"
    folder.mkdir()
    (folder / "items.conf").write_text(CAP_SCHEMA)
    result = _generate(folder, tmp_path / "out")
    assert result.returncode == 2
    assert "items.items keeps 6 definitions" in result.stderr
    assert not (tmp_path / "out").exists()
