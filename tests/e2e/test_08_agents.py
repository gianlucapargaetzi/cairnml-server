"""Section 5 of docs/e2e_testing/E2E.md: enrolling an agent machine, its registration and resources, heartbeat and recovery.

The agent is a real managed daemon on this machine, running in its own HOME with its own device key. Its
resources are checked against the host's own counts. Jobs are section 6 (test_09_jobs.py).
"""

import os
import shutil
import subprocess
import time

import pytest

from support import workspace
from support import agent
from support.agent import AgentMachine, worker_record
from support.workspace import new_name

pytestmark = [pytest.mark.agent, pytest.mark.xdist_group("agent")]

# A negative check: an agent without its key must stay unregistered for this long (about five syncs).
NEGATIVE_WINDOW_SEC = 10


@pytest.fixture(scope="module")
def machine(tmp_path_factory, admin_account, client_home):
    machine = AgentMachine(tmp_path_factory.mktemp("agent-home"), worker_name=new_name("agent"))
    result = agent.init(machine.home, admin_account)
    assert result.returncode == 0, result.output
    machine.start()
    api = workspace.device_api(client_home)
    online = machine.wait_until(lambda: worker_record(api, machine.worker_name), timeout=60)
    assert online, f"the agent never registered; its log ends:\n{machine.log()[-3000:]}"
    yield machine
    machine.stop()


def test_admin_enrols_the_agent_machine_with_its_own_key(machine):
    """Enrolling the agent machine with its own key.

    Expect: the agent's device key is written, with its own device id, readable by its owner only."""
    assert machine.key_path.exists(), "the agent's device key must be written"
    assert machine.device_id.startswith("dev_")
    assert oct(os.stat(machine.key_path).st_mode & 0o777) == "0o600"


def test_agent_device_is_kind_agent(client_home, machine):
    """The agent's device in the device list.

    Expect: the device of the agent is listed as an agent, and it is not revoked."""
    api = workspace.device_api(client_home)
    devices = api.authed("devices.get_all", {})["devices"]
    mine = next(d for d in devices if d["id"] == machine.device_id)
    assert mine["kind"] == "agent"
    assert mine["revoked"] is False


def test_member_cannot_enrol_an_agent(member_account, tmp_path):
    """Only an admin may enrol an agent, because it runs other people's code.

    Expect: a plain user's attempt to enrol an agent is refused and leaves no device key behind.
    """
    machine = AgentMachine(tmp_path / "member-agent", worker_name="e2e-refused")
    machine.home.mkdir()
    result = agent.init(machine.home, member_account)
    assert "only an admin" in result.output or "not enrolled" in result.output, result.output
    assert not machine.key_path.exists()
    assert not (machine.home / "cairnml-agent.conf").exists() or "device_id" not in (
        machine.home / "cairnml-agent.conf").read_text()


def test_agent_registers_as_a_worker_listening_to_its_queue(client_home, machine):
    """The agent registers as a worker, and listens to the default queue.

    Expect: the worker is in the server's worker list, and its queues include the default queue.
    """
    record = worker_record(workspace.device_api(client_home), machine.worker_name)
    assert record is not None
    assert "default" in [q.get("name") for q in record.get("queues", [])], record


def keyless(tmp_path_factory, admin_account):
    """One agent machine whose device key was removed: started once, shared by the two keyless checks (one daemon,
    not two: docs/e2e_testing/E2E.md, speed section, item 5). It is up when its log names the missing key."""
    machine = AgentMachine(tmp_path_factory.mktemp("keyless"), worker_name=new_name("keyless"))
    assert agent.init(machine.home, admin_account).returncode == 0
    machine.key_path.unlink()
    machine.start()
    machine.wait_until(lambda: "device.key" in machine.log() or "enrol" in machine.log().lower(), timeout=30, poll=1)
    yield machine
    machine.stop()


def test_agent_without_its_key_does_not_register(keyless, client_home):
    """A machine whose device key is gone cannot sign in, so it never appears as a worker.

    Expect: over a window of several syncs the worker never appears, and the agent's log says why.
    """
    time.sleep(NEGATIVE_WINDOW_SEC)  # a negative check: the agent must stay unregistered for the whole window
    api = workspace.device_api(client_home)
    assert worker_record(api, keyless.worker_name) is None
    assert "device.key" in keyless.log() or "enrol" in keyless.log().lower(), keyless.log()[-2000:]


def test_agent_without_its_key_says_to_enrol_it(keyless):
    """Defect D7: a machine with no device key logs a raw error instead of 'run cairnml-agent init'.

    Expect: the agent's log names the command to run, cairnml-agent init.
    """
    assert "cairnml-agent init" in keyless.log(), keyless.log()[-2000:]


def _host_gpu_count() -> int:
    if not shutil.which("nvidia-smi"):
        return 0
    out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=30)
    return sum(1 for line in out.stdout.splitlines() if line.startswith("GPU "))
