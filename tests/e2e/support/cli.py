"""Runs the CairnML commands the way a person does, in an isolated HOME, and captures what they print."""

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import settings

BROWSER_DRIVER = Path(__file__).with_name("browser.py")
VENV_BIN = Path(sys.executable).parent


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


def environment(home: Path, login_user: str = "", login_password: str = "") -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{VENV_BIN}{os.pathsep}{env.get('PATH', '')}"
    # The sign-in page opens through webbrowser: a BROWSER command ending in '&' runs in the background,
    # so cairnml-init keeps listening for the sign-in reply while the driver signs in.
    env["BROWSER"] = f"{shlex.quote(sys.executable)} {shlex.quote(str(BROWSER_DRIVER))} %s &"
    env["E2E_LOGIN_USER"] = login_user
    env["E2E_LOGIN_PASSWORD"] = login_password
    env["PYTHONUNBUFFERED"] = "1"
    env["PLAYWRIGHT_BROWSERS_PATH"] = settings.BROWSERS_PATH
    return env


def run(command: str, home: Path, *, login_user="", login_password="", timeout=240, args=(), extra_env=None,
        stdin_text=None) -> Result:
    """Runs a console script from the venv. The sign-in (if any) is the given account."""
    env = environment(home, login_user, login_password)
    env.update(extra_env or {})
    proc = subprocess.run(
        [str(VENV_BIN / command), *args],
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


def python(script: Path, home: Path, *, args=(), timeout=300, extra_env=None) -> Result:
    """Runs a script with the venv's Python, in the isolated HOME, so it sees the enrolled configuration."""
    env = environment(home)
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)
