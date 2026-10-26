# The suite's runner, built inside dind by inside.sh from a staged build context:
#   runner.Dockerfile, requirements.txt, src/cairnml, src/cairnml-agent
# It holds the test venv (the suite, the SDK and the agent from their folders), Chromium for the browser tests, the
# Docker CLI and Compose (they talk to the daemon of dind through its socket), and git for the git tests.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt

# The SDK and the agent are installed from their folders, editable, as the host's venv has them.
COPY src/cairnml /src/cairnml
COPY src/cairnml-agent /src/cairnml-agent
RUN pip install -e "/src/cairnml[dataset]" -e /src/cairnml-agent

# git reads the mounted repository, whose files belong to another user
RUN git config --system --add safe.directory '*'

WORKDIR /workspace/tests/e2e
