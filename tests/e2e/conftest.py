"""Shared fixtures for the end-to-end suite.

The suite runs against its own stack (support/stack.py): a separate compose project, with its own state, accounts and
ports. Nothing here touches deploy/ or the stack it runs. Every session gets its own HOME, so the machine's real
~/.cairnml is never touched, and the test devices are enrolled through the real sign-in page.

Set CAIRNML_E2E_KEEP_STACK=1 to leave the stack running after the session, for debugging.
Set CAIRNML_E2E_REUSE_STACK=1 to keep the stack running for the next session (see docs/e2e_testing/E2E.md, speed section, item 1). A reused
stack is reset at the start of each run, through the suite's own reset (system.e2e_reset, off unless the stack is the
suite's).
Each session writes the report to tests/e2e/reports/ (support/report.py).
"""

import fcntl
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import requests

from support import agent, client, settings, workspace
from support import report as e2e_report
from support.keycloak import Keycloak
from support.stack import REUSE_DIR, IsolatedStack, StackError, reuse_requested

# The browsers are installed in the real home; the SDK and the CLI run with a throwaway one.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", settings.BROWSERS_PATH)

# How long an agent may take to show up as a worker: its first sync is at most a couple of seconds away.
REGISTRATION_TIMEOUT = 60


def pytest_collection_modifyitems(config, items):
    """Tests marked github need a GitHub repository this machine can reach. They are skipped unless
    CAIRNML_E2E_GITHUB_REPO names one (see docs/e2e_testing/E2E.md)."""
    e2e_report.collect(items)
    if settings.github_tests_enabled():
        return
    skip = pytest.mark.skip(reason="needs a reachable GitHub repository: set CAIRNML_E2E_GITHUB_REPO (docs/e2e_testing/E2E.md)")
    for item in items:
        if "github" in item.keywords:
            item.add_marker(skip)


def pytest_runtest_logreport(report):
    """Every phase of every test goes into the report (support/report.py)."""
    e2e_report.record(report)


def pytest_sessionfinish(session, exitstatus):
    e2e_report.write()


@pytest.fixture(scope="session", autouse=True)
def isolated_stack(request):
    """Starts the suite's own stack, and removes it (containers, volumes, state folder) when the session ends, unless
    it is reused. With pytest-xdist every worker needs the same stack, so the workers require a reused one."""
    if hasattr(request.config, "workerinput") and not reuse_requested():
        pytest.exit("Parallel runs (-n) need CAIRNML_E2E_REUSE_STACK=1: every worker must find the same stack.", returncode=2)
    stack = IsolatedStack(keep=bool(os.environ.get("CAIRNML_E2E_KEEP_STACK")))
    try:
        stack.start()
    except StackError as exc:
        stack.stop()
        pytest.exit(f"The test stack could not start:\n{exc}", returncode=2)
    except BaseException:
        stack.stop()  # any other failure still removes what was started
        raise
    try:
        yield stack
    finally:
        stack.stop()


def _health_problems() -> list:
    problems = []
    checks = {
        "CairnML API": (f"{settings.API_URL}/v2.8/login.supported_modes", "post"),
        "web app": (settings.WEB_URL + "/", "get"),
        "sign-in service": (f"{settings.ISSUER}/.well-known/openid-configuration", "get"),
        "object storage": (settings.STORAGE_URL + "/health", "get"),
    }
    for name, (url, method) in checks.items():
        try:
            reply = requests.request(method, url, timeout=10, json={} if method == "post" else None)
            if reply.status_code != 200:
                problems.append(f"{name} answered {reply.status_code} at {url}")
        except requests.RequestException as exc:
            problems.append(f"{name} is not reachable at {url} ({exc.__class__.__name__})")
    return problems


@pytest.fixture(scope="session", autouse=True)
def stack(isolated_stack):
    """Every test needs the stack. If it is not answering, the suite stops here, with the reason."""
    problems = _health_problems()
    if problems:
        pytest.exit("The test stack is not ready:\n  - " + "\n  - ".join(problems), returncode=2)
    return settings


@pytest.fixture(scope="session")
def browser():
    """One headless Chromium for the session; each test signs in in a context of its own."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="session")
def session_home():
    """A throwaway HOME for this session. The SDK, the CLI and the device key all live here."""
    home = Path(tempfile.mkdtemp(prefix="cairnml-e2e-home-"))
    yield home
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(scope="session")
def keycloak(stack):
    return Keycloak()


@pytest.fixture(scope="session")
def admin_account(stack):
    return settings.ADMIN_USER, settings.ADMIN_PASSWORD


@pytest.fixture(scope="session")
def member_account(keycloak):
    """A plain user (not an admin), created for this session and removed afterwards."""
    username = f"{settings.PREFIX}-member-{os.urandom(3).hex()}"
    password = os.urandom(12).hex()
    keycloak.create_user(username, password)
    yield username, password
    keycloak.delete_user(username)


@pytest.fixture(scope="session")
def client_home(session_home, admin_account):
    """This session's client machine, enrolled as the admin through the sign-in page."""
    result = client.init(session_home, admin_account, name="e2e-client")
    assert result.returncode == 0, result.output
    yield session_home
    client.logout(session_home)


@pytest.fixture(scope="session", autouse=True)
def fresh_records(stack, client_home):
    """On a reused stack, removes what the earlier sessions left, with the suite's own reset (system.e2e_reset).

    It runs before any module, so no agent or project of this run exists yet. With pytest-xdist every worker runs this
    fixture, but the workers share one run id: the first to take the lock resets, and the others wait and then skip.
    """
    if not reuse_requested():
        yield
        return
    run_id = os.environ.get("PYTEST_XDIST_TESTRUNUID") or f"single-{os.getpid()}"
    REUSE_DIR.mkdir(parents=True, exist_ok=True)
    marker = REUSE_DIR / f"reset-{run_id}"
    with open(marker, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        if handle.read().strip() != "done":
            try:
                workspace.device_api(client_home).authed("system.e2e_reset", {"prefix": settings.PREFIX})
            except Exception as exc:
                pytest.exit(f"The reused test stack could not be reset: {exc}", returncode=2)
            handle.seek(0)
            handle.truncate()
            handle.write("done")
            handle.flush()
    yield
    if not os.environ.get("PYTEST_XDIST_TESTRUNUID"):
        marker.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def job_agent(tmp_path_factory, admin_account, client_home):
    """One managed agent for the session's job tests: enrolled and started once, shared by the modules that run jobs.
    Starting a second daemon on this machine would stop the first one's job containers, so the modules share this one."""
    from support.agent import AgentMachine, worker_record

    machine = AgentMachine(tmp_path_factory.mktemp("job-agent"), worker_name=workspace.new_name("jobs-agent"))
    assert agent.init(machine.home, admin_account).returncode == 0
    machine.start()
    api = workspace.device_api(client_home)
    assert machine.wait_until(lambda: worker_record(api, machine.worker_name), timeout=REGISTRATION_TIMEOUT), \
        machine.log()[-2000:]
    yield machine
    machine.stop()


@pytest.fixture(scope="session")
def project(client_home):
    """The project this session's tasks, datasets and models are created in. Deleted, with its contents, at the end."""
    api = workspace.device_api(client_home)
    name = workspace.new_name("project")
    project_id = workspace.create_project(api, name)
    yield name
    try:
        workspace.delete_project(api, project_id)
    except Exception:  # cleanup must not hide the failures of the tests themselves
        pass
