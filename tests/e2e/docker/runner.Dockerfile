# The suite's runner, built inside dind by inside.sh from a staged build context:
#   runner.Dockerfile, requirements.txt (tests/e2e/requirements.txt, exact pins), src/cairnml
# It holds the test venv (the suite, the SDK and the agent from their folders), Chromium for the browser tests, the
# Docker CLI and Compose (they talk to the daemon of dind through its socket), and git for the git tests.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates procps libatomic1 \
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

# The SDK, with its agent (cairnml_agent, the extra), is installed from its folder, editable, as the host's venv has it.
COPY src/cairnml /src/cairnml
RUN pip install -e "/src/cairnml[dataset,agent]"

# git reads the mounted repository, whose files belong to another user
RUN git config --system --add safe.directory '*'

WORKDIR /workspace/tests/e2e
