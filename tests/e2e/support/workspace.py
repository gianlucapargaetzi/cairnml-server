"""Running the example scripts as a user would, and the project each session works in."""

import json
import os
from pathlib import Path

from . import cli, settings
from .api import Api

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def script(name: str) -> Path:
    return SCRIPTS / name


def _last_json_line(stdout: str) -> dict:
    """The script's result: its last JSON line. The SDK may print other lines after it (at exit, a GPU monitor notice
    on a machine without a GPU), so the last line itself is not always the result."""
    lines = [line for line in stdout.splitlines() if line.startswith("{")]
    return json.loads(lines[-1])


def run_json(home: Path, script_name: str, args=(), *, timeout=300, extra_env=None) -> dict:
    """Runs an example script in the given home. The script's last JSON line is returned parsed."""
    result = cli.python(script(script_name), home, args=args, timeout=timeout, extra_env=extra_env)
    assert result.returncode == 0, f"{script_name} failed:\n{result.output[-4000:]}"
    return _last_json_line(result.stdout)


def run_json_lines(home: Path, script_name: str, args=(), *, timeout=300) -> list:
    """Runs an example script that records several results in one process: each JSON line is one result."""
    result = cli.python(script(script_name), home, args=args, timeout=timeout)
    assert result.returncode == 0, f"{script_name} failed:\n{result.output[-4000:]}"
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def run_raw(home: Path, script_name: str, args=(), *, timeout=300, extra_env=None):
    """Like run_json, but returns the process result (for the tests that expect a failure)."""
    return cli.python(script(script_name), home, args=args, timeout=timeout, extra_env=extra_env)


def device_api(home: Path) -> Api:
    """The REST client as the device enrolled in this home."""
    from .client import device_id_of, key_path

    return Api(device_id_of(home), key_path(home))


def new_name(label: str) -> str:
    return f"{settings.PREFIX}-{label}-{os.urandom(3).hex()}"


def create_project(api: Api, name: str) -> str:
    return api.authed("projects.create", {"name": name})["id"]


def delete_project(api: Api, project_id: str) -> None:
    api.authed("projects.delete", {"project": project_id, "force": True, "delete_contents": True})
