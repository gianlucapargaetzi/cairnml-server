"""The suite runs against its own stack, and nothing else: none of the addresses of the stack under deploy/ is used,
and the API answering is a container of this suite's compose project.
"""

import subprocess

from support import settings
from support.stack import BIND, HOST

REAL_STACK_PORTS = {8008, 8080, 8180, 9000, 9001}
REAL_STACK_NAMES = ("cairnml", "cairnml-idp", "cairnml-nas")


def _port(url: str) -> int:
    return int(url.rsplit(":", 1)[1].split("/")[0])


def test_the_addresses_are_not_the_ones_of_the_deploy_stack():
    """The suite's addresses are not those of the stack under deploy/.

    Expect: No address uses the ports or the names of the deploy stack.
    """
    for url in (settings.API_URL, settings.WEB_URL, settings.IDP_URL, settings.STORAGE_URL):
        assert _port(url) not in REAL_STACK_PORTS, url
        assert not any(name in url for name in REAL_STACK_NAMES), url


def test_the_api_is_served_by_this_suites_compose_project():
    """The API container is this session's, and it listens on the loopback port the suite uses.

    Expect: Exactly one API container runs in this suite's project, listening on the suite's loopback port.
    """
    names = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={settings.STACK_PROJECT}", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=60).stdout.split()
    apiservers = [n for n in names if "apiserver" in n]
    assert len(apiservers) == 1, names
    env = subprocess.run(["docker", "inspect", apiservers[0], "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
                         capture_output=True, text=True, timeout=60).stdout.splitlines()
    assert f"CAIRNML__apiserver__listen__port={_port(settings.API_URL)}" in env
    assert f"CAIRNML__apiserver__listen__ip={BIND}" in env
    assert settings.API_URL.startswith(f"http://{HOST}:")
