"""`generate` and `check-web` must run where only pyhocon and jinja2 are installed (the CI jobs of the SDK, the web app and
the server's sync job install nothing else); only `check-callers` needs `requests`."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]


def _run_without_requests(code: str) -> subprocess.CompletedProcess:
    # `sys.modules["requests"] = None` makes `import requests` raise ImportError, as in an environment without it
    prefix = "import sys; sys.modules['requests'] = None\n"
    return subprocess.run(
        [sys.executable, "-c", prefix + code], cwd=ROOT, capture_output=True, text=True, env={"PYTHONPATH": str(ROOT)}
    )


def test_the_generator_command_line_imports_without_requests():
    done = _run_without_requests("import apiserver.client_generator.__main__ as m; print('ok')")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_check_callers_names_requests_when_it_is_missing():
    done = _run_without_requests(
        "from apiserver.client_generator.callers import http_session_methods; http_session_methods()"
    )
    assert done.returncode != 0
    assert "requests" in done.stderr
