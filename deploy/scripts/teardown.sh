#!/usr/bin/env bash
# Removes the CairnML stack from this machine. The stack's state lives in CAIRNML_HOME, outside the repository
# (default ~/.local/share/cairnml): data/ (users, databases, logs, object storage, artifacts) and secrets/.
#
#   (no mode)  stop and remove the containers, volumes and images of the stack; keep all state
#   --data     also remove the data (and the data folder of an earlier layout, deploy/data)
#
# Never removed, in any mode: the secrets (secrets/ in CAIRNML_HOME, and the folder an earlier layout used,
# deploy/secrets), deploy/.env, and the GitHub App key, which cannot be created again.
#
# Other options:
#   --prune-docker  images, networks and volumes of other projects on this machine
#   --dry-run       print the plan, change nothing
#   --yes           skip the confirmation phrase
set -euo pipefail
cd "$(dirname "$0")/.."
DEPLOY_DIR="$PWD"

# CAIRNML_HOME may be set in deploy/.env (bootstrap.sh does the same).
if [ -f .env ]; then set -a; . ./.env; set +a; fi
STATE="${CAIRNML_HOME:-$HOME/.local/share/cairnml}"

DRY_RUN=0
ASSUME_YES=0
DATA=0
PRUNE_DOCKER=0

usage() {
  cat <<'USAGE'
Usage: deploy/scripts/teardown.sh [mode] [options]

Modes:
  (none)              stop and remove the containers, volumes and images of the stack; keep all state
  --data              also remove the data: users, databases, logs, object storage, artifacts

Never removed: the secrets, deploy/.env, and the GitHub App key (github-app.pem) and its copies (github_app_key).

Options:
  --dry-run           print what would be removed, change nothing
  --yes               skip the confirmation phrase
  --prune-docker      also run docker system prune -a --volumes (every unused image, network and volume)

USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --data) DATA=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    --prune-docker) PRUNE_DOCKER=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }

# run CMD... : executes the command, or only prints it with --dry-run
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  would run: %s\n' "$*"
  else
    "$@"
  fi
}

# clear_dir DIR : removes everything in DIR except the GitHub App key and its copies, then DIR itself if nothing is
# left. Done in a throwaway container: the files are owned by the users of the stack's containers, which do not exist
# on this machine. The key files are never deleted, so their folders stay.
clear_dir() {
  local dir="$1"
  if [ ! -e "$dir" ]; then
    say "  skip: $dir does not exist"
    return
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  would remove: $dir (the GitHub App key stays, if present)"
    return
  fi
  docker run --rm -v "$dir:/wipe" alpine:3.20 sh -c '
    find /wipe ! -type d ! -name github-app.pem ! -name github_app_key -delete
    find /wipe -mindepth 1 -type d -empty -delete
    find /wipe -type f \( -name github-app.pem -o -name github_app_key \)' \
    | sed 's|^/wipe/|  kept: |'
  say "  cleared: $dir"
  if [ -z "$(ls -A "$dir")" ]; then
    rmdir "$dir"
    say "  removed: $dir"
  fi
}

# The device key signs in as its device (devices.exchange) and revokes it on the server (devices.revoke). Run inside the
# API container, with the key on stdin, so the key never lands in the container. The audience is the stack's public
# address (the proxy on port 80); the forwarded headers make the server use it, as they do for the proxy.
REVOKE_PY='import json, sys, time, urllib.request, uuid
import jwt
from cryptography.hazmat.primitives import serialization

device_id = sys.argv[1]
key = serialization.load_pem_private_key(sys.stdin.buffer.read(), password=None)
PUBLIC_AUD = "http://localhost/cairnml/api"

def call(path, body, token=None):
    headers = {"Content-Type": "application/json", "X-Forwarded-Host": "localhost", "X-Forwarded-Proto": "http"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request("http://127.0.0.1:8008/" + path, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

now = int(time.time())
assertion = jwt.encode({"iss": device_id, "sub": device_id, "aud": PUBLIC_AUD, "iat": now, "exp": now + 120, "jti": uuid.uuid4().hex}, key, algorithm="EdDSA")
status, body = call("devices.exchange", {"assertion": assertion})
if status != 200:
    reason = body.get("meta", {}).get("result_msg", "")
    if "unknown device" in reason or "revoked" in reason:
        print("  already gone on the server:", device_id, "-", reason)
        sys.exit(0)
    print("  could not sign in as", device_id, "-", reason)
    sys.exit(1)
status, body = call("devices.revoke", {"id": device_id}, body["data"]["access_token"])
if status != 200:
    print("  revoke refused:", device_id, "-", body.get("meta", {}).get("result_msg", ""))
    sys.exit(1)
print("  revoked on the server:", device_id)'

# revoke_device KEY_FILE DEVICE_ID : revokes the device on the running server. Skipped when the stack is not running
# (then the device stays on the server until its data is removed with --data).
revoke_device() {
  local key="$1" id="$2"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  would revoke on the server: $id"
    return 0
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx cairnml-apiserver; then
    say "  skip: the API server is not running, so $id stays on the server until --data"
    return 0
  fi
  docker exec -i cairnml-apiserver python3 -c "$REVOKE_PY" "$id" < "$key" \
    || say "  warning: could not revoke $id on the server (it stays until --data)"
}

# device_id_of FILE : the device_id a CairnML config file names, if any
device_id_of() {
  if [ -f "$1" ]; then
    sed -n 's/.*device_id: *"\(dev_[0-9a-f]*\)".*/\1/p' "$1" | head -1
  fi
  return 0
}

# ---------------------------------------------------------------- plan
say "cairnml teardown"
say "deploy folder: $DEPLOY_DIR"
say "state folder:  $STATE"
say ""
say "This removes:"
say "  - the sign-in keys of the CLI and the agent, and the CairnML config files on this machine (~/.cairnml, ~/cairnml.conf and ~/cairnml-agent.conf, if they name our server)"
say "  - the containers, volumes and network of the stack (deploy/compose.yaml)"
say "  - the images cairnml-server-fork:local and the Keycloak image"
[ "$DATA" -eq 1 ] && say "  - the data: $STATE/data and the folders deploy/data (users, databases, logs, object storage, artifacts)"
[ "$PRUNE_DOCKER" -eq 1 ] && say "  - every unused Docker image, network and volume on this machine, including other projects (--prune-docker)"
say ""
say "Kept: the source code, the fetched repositories, docs, the /etc/hosts line, the other Docker projects (unless --prune-docker),"
[ "$DATA" -eq 0 ] && say "      all data,"
say "      the secrets, deploy/.env, and the GitHub App key (always)."
say ""

if [ "$DRY_RUN" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ]; then
  say "Type 'delete cairnml' to continue:"
  read -r answer
  if [ "$answer" != "delete cairnml" ]; then
    say "not confirmed; nothing removed"
    exit 1
  fi
fi

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

# ---------------------------------------------------------------- 1. this machine's access
say "1. Revoking access: the server first (the stack must be running), then this machine"
agent_id=$(device_id_of "$HOME/cairnml-agent.conf")
if [ -n "$agent_id" ] && [ -f "$HOME/.cairnml/agent/device.key" ]; then
  revoke_device "$HOME/.cairnml/agent/device.key" "$agent_id"
fi
client_id=$(device_id_of "$HOME/cairnml.conf")
if [ -n "$client_id" ] && [ -f "$HOME/.cairnml/device.key" ]; then
  revoke_device "$HOME/.cairnml/device.key" "$client_id"
fi
if [ -f "$HOME/.cairnml/device.key" ]; then
  run rm -f "$HOME/.cairnml/device.key"
fi
if [ -f "$HOME/.cairnml/.session.json" ]; then
  run rm -f "$HOME/.cairnml/.session.json"
fi
# The agent's own device key (the agent signs in with it, not with the CLI's key).
if [ -f "$HOME/.cairnml/agent/device.key" ]; then
  run rm -f "$HOME/.cairnml/agent/device.key"
  say "  the agent's device key"
fi
if [ -f "$HOME/cairnml.conf" ] && grep -q "cairnml" "$HOME/cairnml.conf"; then
  run rm -f "$HOME/cairnml.conf"
  say "  ~/cairnml.conf names the cairnml server"
fi
# The agent config names the server and holds the device id. It can also hold a git token, so it goes with the key.
if [ -f "$HOME/cairnml-agent.conf" ] && grep -q "cairnml" "$HOME/cairnml-agent.conf"; then
  run rm -f "$HOME/cairnml-agent.conf"
  say "  ~/cairnml-agent.conf names the cairnml server (it can hold git credentials)"
fi
say "  revoked devices stay listed on the server until the data is removed (--data)"

# ---------------------------------------------------------------- 2. containers and images
# deploy/.env is read by compose from this folder, because compose.yaml is here.
say "2. Stopping and removing the containers"
run docker compose -f compose.yaml down -v --remove-orphans
image=cairnml-server-fork:local
if [ "$DRY_RUN" -eq 1 ] || docker image inspect "$image" >/dev/null 2>&1; then
  run docker image rm -f "$image"
fi

# ---------------------------------------------------------------- 3. data
if [ "$DATA" -eq 1 ]; then
  say "3. Removing the data (users, databases, logs, object storage, artifacts)"
  clear_dir "$STATE/data"
  if [ "$DEPLOY_DIR/data" != "$STATE/data" ]; then
    say "  the folder of an earlier layout:"
    clear_dir "$DEPLOY_DIR/data"
  fi
else
  say "3. Data kept (use --data to remove it)"
fi

# ---------------------------------------------------------------- 4. the rest of Docker
if [ "$PRUNE_DOCKER" -eq 1 ]; then
  say "4. Pruning Docker: every unused image, network and volume"
  run docker system prune -a --volumes -f
else
  say "4. Other Docker projects kept (use --prune-docker to prune everything)"
fi

say ""
if [ "$DRY_RUN" -eq 1 ]; then
  say "Dry run: nothing was changed."
else
  say "Done. Run deploy/scripts/bootstrap.sh to set the stack up again; it creates the data and secrets folders."
fi
