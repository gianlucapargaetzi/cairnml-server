#!/bin/sh
# Runs the end-to-end suite in the isolated environment (docs/e2e_testing/E2E.md, The isolated environment). The host runs one container, dind, and nothing
# else: the runner and the CairnML stack run inside it (inside.sh).
#
#   tests/e2e/docker/run.sh                       the whole suite
#   tests/e2e/docker/run.sh -k artifacts          one area
#   tests/e2e/docker/run.sh -n 4 -m "not restart" the parallel run, then the restart test on its own
#   tests/e2e/docker/run.sh --stop-stack          stop the CairnML stack, keep the environment
#   tests/e2e/docker/run.sh --down                remove the environment: its containers, images and volumes
set -e
cd "$(dirname "$0")"

if [ "$1" = "--down" ]; then
  docker compose -f compose.yaml down -v --remove-orphans
  exit 0
fi

# The web app is built into the stack's image: put the commit that docker/build/web-ref names in web/.
../../../scripts/fetch-sources.sh
docker compose -f compose.yaml up -d --build --wait dind
# The suite writes into the repository (reports, caches), so it runs as the user who starts it, not as root.
exec docker compose -f compose.yaml exec -T -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" dind sh /workspace/tests/e2e/docker/inside.sh "$@"
