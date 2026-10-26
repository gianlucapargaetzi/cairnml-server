#!/bin/sh
# Runs inside the dind container (run.sh starts it on the host, and runs this there). It builds the runner image in this
# daemon, then runs the suite in a runner container of this daemon.
#
# The runner shares the network namespace of dind, as the stack does: the stack is reached at 127.0.0.1, and a job
# container reaches the server through the loopback rule of the agent (cairnml-agent, worker.py).
#
#   inside.sh                  the suite; the arguments go to pytest
#   inside.sh --stop-stack     stop the CairnML stack, keep the environment
set -e

STAGE=/tmp/runner-build
IMAGE=cairnml-e2e-runner
# run.sh passes the user who started it: the runner runs as that user, so what it writes into the repository is theirs.
: "${HOST_UID:?run.sh passes the host user}" "${HOST_GID:?run.sh passes the host user}"
DOCKER_GID=$(stat -c %g /var/run/docker.sock)

# the folders the runner's environment points at: TMPDIR (Playwright and the suite write there), the stack's state,
# and the home and caches of the runner. Only the top level of each is owned by the runner: the stack's data inside
# /e2e-state belongs to its containers.
mkdir -p /e2e-state/tmp /e2e-state/stack /e2e-cache/home
chown "$HOST_UID:$HOST_GID" /e2e-state /e2e-state/tmp /e2e-state/stack /e2e-cache /e2e-cache/home

rm -rf "$STAGE"
mkdir -p "$STAGE/src"
cp /workspace/tests/e2e/docker/runner.Dockerfile /workspace/tests/e2e/docker/requirements.txt "$STAGE/"
cp -r /workspace/cairnml "$STAGE/src/cairnml"
cp -r /workspace/cairnml-agent "$STAGE/src/cairnml-agent"
# build output is not source: a stale build/ folder would be packed into the wheel instead of the sources
rm -rf "$STAGE/src/cairnml/build" "$STAGE/src/cairnml/dist" "$STAGE/src/cairnml-agent/build" "$STAGE/src/cairnml-agent/dist"
docker build -q -t "$IMAGE" -f "$STAGE/runner.Dockerfile" "$STAGE" >/dev/null

RUN="docker run --rm -i --network host \
  --user $HOST_UID:$HOST_GID --group-add $DOCKER_GID \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /workspace:/workspace \
  -v /e2e-state:/e2e-state \
  -v /e2e-cache:/e2e-cache \
  -e HOME=/e2e-cache/home \
  -e XDG_CACHE_HOME=/e2e-cache \
  -e TMPDIR=/e2e-state/tmp \
  -e CAIRNML_E2E_STATE_DIR=/e2e-state/stack \
  -e CAIRNML_E2E_SHARED_ROOT=/e2e-state \
  -e CAIRNML_E2E_REUSE_STACK=1 \
  -e CAIRNML_E2E_HOST=127.0.0.1 \
  -e CAIRNML_E2E_BIND=127.0.0.1 \
  -w /workspace/tests/e2e"

case "$1" in
  --stop-stack)
    exec $RUN "$IMAGE" python -m support.stack --stop
    ;;
esac

exec $RUN "$IMAGE" pytest --basetemp=/e2e-state/pytest "$@"
