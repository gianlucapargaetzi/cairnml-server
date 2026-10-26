#!/usr/bin/env bash
# One-time setup of the sign-in service (Keycloak) and the CairnML server on this machine.
# Safe to run again: it keeps existing secrets, and brings the CairnML clients in step with the realm.
set -euo pipefail
cd "$(dirname "$0")/.."

# The web app is built into the server image: put the commit that docker/build/web-ref names in web/.
../scripts/fetch-sources.sh

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
docker compose version >/dev/null || { echo "docker compose v2.20+ is required" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^KC_ADMIN_PASSWORD=.*|KC_ADMIN_PASSWORD=$(openssl rand -hex 16)|" .env
  echo "wrote deploy/.env with a generated Keycloak admin password"
fi
set -a; . ./.env; set +a

# The address that browsers, the SDK and agents use to reach this server. Empty in deploy/.env: this machine's IPv4
# address, written back to deploy/.env so that later runs keep it.
if [ -z "${CAIRNML_HOST:-}" ]; then
  CAIRNML_HOST=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)
  [ -n "$CAIRNML_HOST" ] || { echo "could not find this machine's IP address: set CAIRNML_HOST in deploy/.env" >&2; exit 1; }
  if grep -q '^CAIRNML_HOST=' .env; then
    sed -i "s|^CAIRNML_HOST=.*|CAIRNML_HOST=$CAIRNML_HOST|" .env
  else
    echo "CAIRNML_HOST=$CAIRNML_HOST" >> .env
  fi
  echo "CAIRNML_HOST=$CAIRNML_HOST (written to deploy/.env)"
fi
export CAIRNML_HOST

# The stack's state (data and secrets) lives outside the repository: CAIRNML_HOME, default ~/.local/share/cairnml.
# Set it in deploy/.env to put it somewhere else. The GitHub App key used to be deploy/secrets/github-app.pem: it is
# copied here if it is not there yet. The old file is kept, so nothing is lost if the copy goes wrong.
export CAIRNML_HOME="${CAIRNML_HOME:-$HOME/.local/share/cairnml}"
STATE="$CAIRNML_HOME"
if [ ! -f "$STATE/secrets/github-app.pem" ] && [ -f secrets/github-app.pem ]; then
  mkdir -p "$STATE/secrets"
  cp -p secrets/github-app.pem "$STATE/secrets/github-app.pem"
  echo "copied the GitHub App key to $STATE/secrets/ (the old copy in deploy/secrets is kept)"
fi
mkdir -p "$STATE/secrets/cairnml"

# Remove this stack's containers and networks first, so a rerun starts clean and no stale or duplicate containers are
# left behind. The data stays: it lives in $STATE (bind mounts), and -v is not used, so volumes are kept too.
docker compose down --remove-orphans

# The realm names this server by CAIRNML_HOST (the redirect addresses of the web app and the API). Keycloak imports
# the rendered copy, and the clients below are updated from it.
mkdir -p "$STATE/idp"
sed "s|http://cairnml:|http://$CAIRNML_HOST:|g" idp/realm-cairnml.json > "$STATE/idp/realm-cairnml.json"

# The identity provider keeps its data in a host folder; its container runs as uid 1000.
mkdir -p "$STATE/data/idp"
docker run --rm -v "$STATE/data:/data" alpine:3.20 sh -c 'chown 1000:1000 /data/idp && chmod 700 /data/idp'

docker compose up -d cairnml-idp

# One admin login for both Keycloak's master realm and the cairnml realm: KC_ADMIN and KC_ADMIN_PASSWORD
# from deploy/.env, applied on every run so the password in .env is the one that works. Nothing is printed.
KC_USER_SETUP='
set -e
K=/opt/keycloak/bin/kcadm.sh
ok=0
for _ in $(seq 1 90); do
  if $K config credentials --server http://localhost:8180 --realm master \
       --user "$KC_MASTER_USER" --password "$KC_MASTER_PW" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "keycloak did not become ready" >&2; exit 1; }
$K set-password -r master --username "$ADMIN_USER" --new-password "$ADMIN_PW" >/dev/null
# The stack runs on a LAN reached by its address over plain HTTP. The default SSL policy of the master realm
# would refuse that, and the admin console signs in there.
$K update realms/master -s sslRequired=NONE
if $K get users -r cairnml -q username="$ADMIN_USER" --fields username | grep -q "\"username\""; then
  $K set-password -r cairnml --username "$ADMIN_USER" --new-password "$ADMIN_PW" >/dev/null
else
  $K create users -r cairnml -s username="$ADMIN_USER" -s enabled=true -s emailVerified=true \
    -s email="$ADMIN_USER@example.test" -s firstName=Admin -s lastName=User >/dev/null
  $K add-roles -r cairnml --uusername "$ADMIN_USER" --rolename cairnml-admin
  $K set-password -r cairnml --username "$ADMIN_USER" --new-password "$ADMIN_PW"
fi
'
docker compose exec -T \
  -e KC_MASTER_USER="$KC_ADMIN" -e KC_MASTER_PW="$KC_ADMIN_PASSWORD" \
  -e ADMIN_USER="$KC_ADMIN" -e ADMIN_PW="$KC_ADMIN_PASSWORD" \
  cairnml-idp sh -c "$KC_USER_SETUP" >/dev/null
echo "admin login set from deploy/.env: user $KC_ADMIN, same password for Keycloak and CairnML"

# CairnML secrets, written once and kept. Nothing is printed.
for name in token_secret session_secret s3_secret_key; do
  if [ ! -s "$STATE/secrets/cairnml/$name" ]; then
    openssl rand -base64 48 | tr -d '\n' > "$STATE/secrets/cairnml/$name"
    chmod 600 "$STATE/secrets/cairnml/$name"
    echo "wrote $STATE/secrets/cairnml/$name"
  fi
done
if [ -f "$STATE/secrets/github-app.pem" ]; then
  cp "$STATE/secrets/github-app.pem" "$STATE/secrets/cairnml/github_app_key"
  chmod 600 "$STATE/secrets/cairnml/github_app_key"
fi

# CairnML keeps its data in host folders too ($CAIRNML_HOME/data/cairnml/). Mongo and Redis run as uid 999,
# Elasticsearch as 1000.
mkdir -p "$STATE/data/cairnml/mongo/db" "$STATE/data/cairnml/mongo/configdb" "$STATE/data/cairnml/redis" \
  "$STATE/data/cairnml/elastic" "$STATE/data/cairnml/logs" "$STATE/data/rustfs"
docker run --rm -v "$STATE/data/cairnml:/data" alpine:3.20 sh -c '
  chown -R 999:999 /data/mongo /data/redis &&
  chown -R 1000:1000 /data/elastic /data/logs'
# RustFS runs as uid 10001.
docker run --rm -v "$STATE/data/rustfs:/data" alpine:3.20 sh -c 'chown -R 10001:10001 /data'

# The realm is imported only on Keycloak's first start, so the CairnML clients are created here, and
# updated to the realm file on every run. The directory client's secret is written once, to $CAIRNML_HOME/secrets/cairnml.
clients_dir=$(mktemp -d)
python3 - "$clients_dir" "$STATE/idp/realm-cairnml.json" <<'PY'
import json, pathlib, sys
realm = json.load(open(sys.argv[2]))
out = pathlib.Path(sys.argv[1])
for client in realm["clients"]:
    if client["clientId"].startswith("cairnml-"):
        (out / (client["clientId"] + ".json")).write_text(json.dumps(client))
PY
docker compose exec -T cairnml-idp rm -rf /tmp/cairnml-clients
docker compose cp "$clients_dir" cairnml-idp:/tmp/cairnml-clients >/dev/null
rm -rf "$clients_dir"

CAIRNML_KC_SETUP='
set -e
K=/opt/keycloak/bin/kcadm.sh
ok=0
for _ in $(seq 1 90); do
  if $K config credentials --server http://localhost:8180 --realm master \
       --user "$KC_MASTER_USER" --password "$KC_MASTER_PW" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "keycloak did not become ready" >&2; exit 1; }
for file in /tmp/cairnml-clients/*.json; do
  id=$(basename "$file" .json)
  existing=$($K get clients -r cairnml -q clientId="$id" --fields id \
    | sed -n "s/.*\"id\" *: *\"\([^\"]*\)\".*/\1/p" | head -n1)
  if [ -n "$existing" ]; then
    $K update "clients/$existing" -r cairnml -f "$file" >/dev/null
  else
    $K create clients -r cairnml -f "$file" >/dev/null
  fi
done
$K add-roles -r cairnml --uusername service-account-cairnml-server \
  --cclientid realm-management --rolename view-users >/dev/null 2>&1 || true
uuid=$($K get clients -r cairnml -q clientId=cairnml-server --fields id \
  | sed -n "s/.*\"id\" *: *\"\([^\"]*\)\".*/\1/p" | head -n1)
$K get "clients/$uuid/client-secret" -r cairnml --fields value \
  | sed -n "s/.*\"value\" *: *\"\([^\"]*\)\".*/\1/p"
'
directory_secret=$(docker compose exec -T \
  -e KC_MASTER_USER="$KC_ADMIN" -e KC_MASTER_PW="$KC_ADMIN_PASSWORD" \
  cairnml-idp sh -c "$CAIRNML_KC_SETUP")
if [ ! -s "$STATE/secrets/cairnml/idp_directory_secret" ]; then
  if [ -z "$directory_secret" ]; then
    echo "could not read the cairnml-server client secret from Keycloak" >&2
    exit 1
  fi
  printf '%s' "$directory_secret" > "$STATE/secrets/cairnml/idp_directory_secret"
  chmod 600 "$STATE/secrets/cairnml/idp_directory_secret"
  echo "wrote $STATE/secrets/cairnml/idp_directory_secret"
fi

# The CairnML containers run as uid 1000 (the server image's user): they read these secrets, written above.
docker run --rm -v "$STATE/secrets/cairnml:/secrets" alpine:3.20 chown -R 1000:1000 /secrets

docker compose up -d --build

echo "waiting for the CairnML server..."
for _ in $(seq 1 90); do
  curl -fsS -X POST "http://$CAIRNML_HOST:8008/v2.8/login.supported_modes" \
    -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1 && { echo "CairnML server is up"; break; }
  sleep 2
done
echo "CairnML: web http://$CAIRNML_HOST:8080, API http://$CAIRNML_HOST:8008"
echo "next: sign in as $KC_ADMIN, then follow the client and agent steps in README.md"
