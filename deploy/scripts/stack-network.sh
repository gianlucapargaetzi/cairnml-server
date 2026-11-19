#!/usr/bin/env bash
# The stack network's subnet check, sourced by bootstrap.sh (and by its tests). Defines functions only.
#
# deploy/compose.yaml pins the subnet of the `cairnml` network (the proxy's address must lie inside the trusted proxy
# range). A stack made before that pin has the network with the subnet Docker picked then. Docker cannot change a
# network's subnet while containers use it, and compose tries to remove and recreate the network on the next `up`,
# which fails with "network cairnml has active endpoints" and leaves the sign-in service stopped.

# Prints "<docker name> <subnet>" of the compose network KEY ($1), as `docker compose config` resolves it: nothing
# when the compose file gives that network no subnet. The subnet is read from the compose file, not repeated here.
compose_network_subnet() {
  local config
  config=$(docker compose config --format json) || return 1
  printf '%s' "$config" | python3 -c '
import json, sys
network = json.load(sys.stdin)["networks"][sys.argv[1]]
subnets = [entry["subnet"] for entry in (network.get("ipam") or {}).get("config") or [] if "subnet" in entry]
if subnets:
    print(network["name"], subnets[0])
' "$1"
}

# When the network KEY ($1, default cairnml) exists with a subnet other than the compose file's, stops the stack with
# `docker compose down` so that compose creates the network again with the right subnet. Nothing else changes: no
# volume, host folder, secret or image is removed (no -v, no --rmi). Does nothing when the network does not exist or
# already has the subnet, so a second run changes nothing. Returns 1, having stopped nothing, when docker cannot be asked.
recreate_network_if_subnet_differs() {
  local key="${1:-cairnml}" wanted name subnet listed existing
  wanted=$(compose_network_subnet "$key") || { echo "cannot read the network $key from 'docker compose config'" >&2; return 1; }
  [ -n "$wanted" ] || return 0
  name="${wanted% *}"
  subnet="${wanted#* }"
  # The name filter matches part of a name (cairnml also matches cairnml_frontend): only the exact name counts.
  listed=$(docker network ls --filter "name=$name" --format '{{.Name}}') || { echo "cannot list the Docker networks" >&2; return 1; }
  printf '%s\n' "$listed" | grep -qxF "$name" || return 0
  existing=$(docker network inspect "$name" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}') \
    || { echo "cannot inspect the Docker network $name" >&2; return 1; }
  case " $existing" in *" $subnet "*) return 0 ;; esac
  echo "The network $name has the subnet ${existing% }, and deploy/compose.yaml pins $subnet. Docker cannot change the subnet of a"
  echo "network that containers use, so the stack is stopped once with 'docker compose down' and its network is created again below."
  echo "Only the containers and networks are removed: the data, the secrets and the images are kept (no -v)."
  docker compose down
}
