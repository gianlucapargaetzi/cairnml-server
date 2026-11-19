"""The bootstrap recreates the stack network when its subnet differs from the one deploy/compose.yaml pins.

The decision lives in deploy/scripts/stack-network.sh, which bootstrap.sh sources. The tests run it with a fake `docker`
command in PATH that answers from files and records every call, so no daemon is needed.
"""

import json
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
LIBRARY = REPO / "deploy" / "scripts" / "stack-network.sh"
BOOTSTRAP = REPO / "deploy" / "scripts" / "bootstrap.sh"
COMPOSE_FILE = REPO / "deploy" / "compose.yaml"

PINNED_SUBNET = yaml.safe_load(COMPOSE_FILE.read_text())["networks"]["cairnml"]["ipam"]["config"][0]["subnet"]

# Answers like `docker`, from FAKE_DIR: networks.txt (one name per line), subnets.txt (what `network inspect` prints),
# config.json (what `compose config --format json` prints); `inspect-fails` and `ls-fails` make that command fail.
FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DIR/calls.txt"
case "$1 $2" in
  "compose config") cat "$FAKE_DIR/config.json" ;;
  "network ls")
    [ ! -e "$FAKE_DIR/ls-fails" ] || { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
    cat "$FAKE_DIR/networks.txt" ;;
  "network inspect")
    [ ! -e "$FAKE_DIR/inspect-fails" ] || { echo "Error response from daemon: inspect failed" >&2; exit 1; }
    cat "$FAKE_DIR/subnets.txt" ;;
  "compose down") ;;
  *) echo "unexpected docker call: $*" >&2; exit 99 ;;
esac
"""


def compose_config_json(networks=None):
    """What `docker compose config --format json` prints for the real compose file (the part the check reads)."""
    declared = yaml.safe_load(COMPOSE_FILE.read_text())["networks"]
    config = {"name": "cairnml", "networks": {}}
    for key, network in declared.items():
        network = network or {}
        config["networks"][key] = {"name": network.get("name", f"cairnml_{key}"), "ipam": network.get("ipam", {})}
    if networks is not None:
        config["networks"] = networks
    return json.dumps(config)


@pytest.fixture
def stack(tmp_path):
    """A fake docker in PATH. Returns run(...), which sources the library and calls the check."""
    fake = tmp_path / "fake"
    fake.mkdir()
    docker = fake / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    (fake / "networks.txt").write_text("")
    (fake / "subnets.txt").write_text("")
    (fake / "config.json").write_text(compose_config_json())

    class Stack:
        dir = fake

        def set(self, name, text):
            (fake / name).write_text(text)

        def run(self, *args):
            env = {"PATH": f"{fake}:/usr/bin:/bin", "FAKE_DIR": str(fake), "HOME": str(tmp_path)}
            command = f'. "{LIBRARY}"; recreate_network_if_subnet_differs {" ".join(args)}'
            result = subprocess.run(
                ["bash", "-c", command], check=False, env=env, capture_output=True, text=True, timeout=30
            )
            calls = (fake / "calls.txt").read_text().splitlines() if (fake / "calls.txt").exists() else []
            return result, calls

    return Stack()


def downs(calls):
    return [call for call in calls if call.startswith("compose down")]


def test_the_library_exists_and_bootstrap_uses_it():
    assert LIBRARY.is_file()
    text = BOOTSTRAP.read_text()
    assert "stack-network.sh" in text
    # The check runs before the first service is started.
    assert text.index("recreate_network_if_subnet_differs") < text.index("docker compose up -d cairnml-idp")


def test_no_network_does_nothing(stack):
    result, calls = stack.run()
    assert result.returncode == 0, result.stderr
    assert downs(calls) == []
    assert not any(call.startswith("network inspect") for call in calls)


def test_a_network_of_another_name_is_not_ours(stack):
    # `docker network ls --filter name=cairnml` also lists cairnml_frontend: only the exact name counts.
    stack.set("networks.txt", "cairnml_frontend\nbridge\n")
    stack.set("subnets.txt", "10.201.2.0/24 \n")
    result, calls = stack.run()
    assert result.returncode == 0, result.stderr
    assert downs(calls) == []
    assert not any(call.startswith("network inspect") for call in calls)


def test_matching_subnet_does_nothing(stack):
    stack.set("networks.txt", "cairnml\n")
    stack.set("subnets.txt", f"{PINNED_SUBNET} \n")
    result, calls = stack.run()
    assert result.returncode == 0, result.stderr
    assert downs(calls) == []
    assert result.stdout == ""


def test_matching_subnet_among_several_does_nothing(stack):
    stack.set("networks.txt", "cairnml\n")
    stack.set("subnets.txt", f"fd00::/64 {PINNED_SUBNET} \n")
    result, calls = stack.run()
    assert result.returncode == 0, result.stderr
    assert downs(calls) == []


def test_different_subnet_stops_the_stack_once_without_removing_data(stack):
    stack.set("networks.txt", "cairnml\n")
    stack.set("subnets.txt", "10.201.2.0/24 \n")
    result, calls = stack.run()
    assert result.returncode == 0, result.stderr
    assert downs(calls) == ["compose down"]
    # No option at all: in particular no -v/--volumes (state is on host folders and in named volumes) and no --rmi.
    assert "-v" not in downs(calls)[0].split()
    assert "--volumes" not in downs(calls)[0]
    assert "--rmi" not in downs(calls)[0]
    # It says what and why.
    assert "10.201.2.0/24" in result.stdout
    assert PINNED_SUBNET in result.stdout
    assert "docker compose down" in result.stdout
    assert "kept" in result.stdout


def test_the_wanted_subnet_is_read_from_the_compose_config_not_hard_coded(stack):
    other = {"cairnml": {"name": "cairnml", "ipam": {"config": [{"subnet": "172.31.7.0/24"}]}}}
    stack.set("config.json", compose_config_json(other))
    stack.set("networks.txt", "cairnml\n")
    stack.set("subnets.txt", f"{PINNED_SUBNET} \n")
    result, calls = stack.run()
    assert result.returncode == 0, result.stderr
    assert downs(calls) == ["compose down"]
    assert "172.31.7.0/24" in result.stdout
    # And a network that matches that other subnet is left alone.
    stack.set("subnets.txt", "172.31.7.0/24 \n")
    (stack.dir / "calls.txt").unlink()
    result, calls = stack.run()
    assert result.returncode == 0, result.stderr
    assert downs(calls) == []


def test_a_compose_file_without_a_subnet_does_nothing(stack):
    stack.set("config.json", compose_config_json({"cairnml": {"name": "cairnml", "ipam": {}}}))
    stack.set("networks.txt", "cairnml\n")
    stack.set("subnets.txt", "10.201.2.0/24 \n")
    result, calls = stack.run()
    assert result.returncode == 0, result.stderr
    assert downs(calls) == []


def test_a_failing_inspect_stops_the_bootstrap_and_stops_nothing(stack):
    stack.set("networks.txt", "cairnml\n")
    (stack.dir / "inspect-fails").write_text("")
    result, calls = stack.run()
    assert result.returncode != 0
    assert downs(calls) == []
    assert "cairnml" in result.stderr


def test_a_failing_network_listing_stops_the_bootstrap_and_stops_nothing(stack):
    (stack.dir / "ls-fails").write_text("")
    result, calls = stack.run()
    assert result.returncode != 0
    assert downs(calls) == []


def test_an_unreadable_compose_config_stops_the_bootstrap_and_stops_nothing(stack):
    stack.set("config.json", "not json")
    stack.set("networks.txt", "cairnml\n")
    stack.set("subnets.txt", "10.201.2.0/24 \n")
    result, calls = stack.run()
    assert result.returncode != 0
    assert downs(calls) == []


def test_a_second_run_changes_nothing(stack):
    # First run: old subnet, the stack is stopped. Compose then recreates the network with the pinned subnet.
    stack.set("networks.txt", "cairnml\n")
    stack.set("subnets.txt", "10.201.2.0/24 \n")
    first, calls = stack.run()
    assert first.returncode == 0, first.stderr
    assert len(downs(calls)) == 1
    stack.set("subnets.txt", f"{PINNED_SUBNET} \n")
    second, calls = stack.run()
    assert second.returncode == 0, second.stderr
    assert len(downs(calls)) == 1
    assert second.stdout == ""
