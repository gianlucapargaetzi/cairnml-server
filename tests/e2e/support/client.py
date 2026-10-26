"""A person's machine as the CairnML SDK enrols it: cairnml-init, cairnml-whoami and cairnml-logout."""

import re
from pathlib import Path

from . import cli, settings


def key_path(home: Path) -> Path:
    return home / ".cairnml" / "device.key"


def device_id_of(home: Path) -> str:
    """The device id this home is enrolled as, read from its configuration file."""
    conf = home / "cairnml.conf"
    if not conf.is_file():
        return ""
    match = re.search(r'device_id\s*[:=]\s*"?(dev_[0-9a-f]{32})', conf.read_text())
    return match.group(1) if match else ""


def init(home: Path, account, *, server: str = None, name: str = "e2e-machine", force: bool = False):
    """cairnml-init, signed in as account (user, password) through the sign-in page."""
    user, password = account
    args = ["--server", server or settings.API_URL, "--name", name]
    if force:
        args.append("--force")
    return cli.run("cairnml-init", home, login_user=user, login_password=password, args=args)


def whoami(home: Path):
    return cli.run("cairnml-whoami", home)


def logout(home: Path):
    return cli.run("cairnml-logout", home)
