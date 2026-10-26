"""docs/e2e_testing/E2E.md, UI checks on runs: the scalars chart and its hover values, the console, debug samples, the comparison of
runs, the dashboards, and live job status. Each test signs in through the sign-in form in its own browser context.

The selectors are the ones of this build: the run's tabs are links ending in /output/<tab>, the project's tabs are
OVERVIEW, WORKLOADS, TASKS, EXPERIMENTS and MODELS.

Waits are for the thing the test checks (a selector, a changed picture), with a ceiling of 10 s, not for the network to
go quiet: the app keeps polling, so 'networkidle' would always run to its ceiling (docs/e2e_testing/E2E.md, speed section, item 9).
"""

import json
import re
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from support import jobs, settings, workspace
from support.web import sign_in
from support.workspace import new_name, run_json

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("agent")]

CONSOLE_LINE = "console line from the example run"
CHECK_TIMEOUT_MS = 10_000

# the live job's script runs from outside the repository: a script inside the checkout would record the checkout's
# origin as the job's repository, and the job's container cannot clone that (test_09 does the same)
LIVE_JOB_FOLDER = Path(tempfile.mkdtemp(prefix="e2e-live-job-"))
for _name in ("common.py", "job_script.py"):
    shutil.copy(workspace.script(_name), LIVE_JOB_FOLDER / _name)
LIVE_JOB_SCRIPT = LIVE_JOB_FOLDER / "job_script.py"


@pytest.fixture(scope="module")
def runs(client_home, project, tmp_path_factory):
    """Two runs of the same metrics in one project: the run to look at, and a second one to compare with. One process
    records both (docs/e2e_testing/E2E.md, speed section, item 12)."""
    folder = tmp_path_factory.mktemp("web-runs")
    (folder / "runs.json").write_text(json.dumps([{"name": new_name("web-run"), "steps": 100},
                                                  {"name": new_name("web-run-2"), "steps": 50}]))
    first, second = workspace.run_json_lines(client_home, "track_example.py",
                                             ["--project", project, "--batch", folder / "runs.json"])
    api = workspace.device_api(client_home)
    project_id = api.authed("projects.get_all", {"name": f"^{project}$"})["projects"][0]["id"]
    return {"project_id": project_id, "first": first["task_id"], "second": second["task_id"]}


@pytest.fixture(scope="module")
def signed_in_page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = sign_in(context)
    yield page
    context.close()


def _open(page, path: str):
    """Opens a page. The caller waits for the content it checks."""
    page.goto(settings.WEB_URL + path)
    page.wait_for_load_state("domcontentloaded", timeout=CHECK_TIMEOUT_MS)


def _until(check, timeout: float = 10.0, poll: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(poll)
    return False


def _task_path(runs, tab: str) -> str:
    return f"/projects/{runs['project_id']}/tasks/{runs['first']}/output/{tab}"


def test_scalars_page_draws_both_series_of_the_loss(runs, signed_in_page):
    """The scalars page of a run.

    Expect: the scalars page names both loss series, shows the accuracy single value, and draws a chart."""
    _open(signed_in_page, _task_path(runs, "scalars"))
    signed_in_page.wait_for_selector("text=loss", timeout=CHECK_TIMEOUT_MS)
    # the sidebar names the loss before the chart has its data: wait for the series names the legend shows
    signed_in_page.wait_for_selector("text=train", timeout=CHECK_TIMEOUT_MS)
    body = signed_in_page.inner_text("body")
    assert "loss" in body
    signed_in_page.screenshot(path=str(Path(__file__).parent / "reports" / "scalars-page.png"), full_page=True)
    assert "train" in body and "val" in body, "the legend must name both series"
    assert "accuracy" in body and "0.93" in body, "the single value must be in the summary"
    assert signed_in_page.locator("canvas, svg").count() > 0


def test_hovering_the_chart_shows_a_tooltip(runs, signed_in_page):
    """The tooltip is drawn on the chart's canvas, so its values are not text the page exposes: the suite checks the
    stored values through the API (test_04). Here the check is that the pointer over the plot changes what is drawn
    (the guide line and the tooltip), and that moving it away changes it back.

    Expect: the picture of the plot changes when the pointer moves over it.
    """
    _open(signed_in_page, _task_path(runs, "scalars"))
    axis = signed_in_page.locator("text=Iterations").first
    axis.wait_for(timeout=CHECK_TIMEOUT_MS)
    box = axis.bounding_box()
    centre_x, plot_y = box["x"] + box["width"] / 2, box["y"] - 190
    clip = {"x": centre_x - 330, "y": plot_y - 150, "width": 480, "height": 330}
    signed_in_page.mouse.move(5, 5)
    idle = signed_in_page.screenshot(clip=clip)
    signed_in_page.mouse.move(centre_x - 200, plot_y)
    signed_in_page.mouse.move(centre_x - 150, plot_y)
    changed = _until(lambda: signed_in_page.screenshot(clip=clip) != idle, timeout=5)
    assert changed, "hovering over the plot must draw the tooltip and the guide line"


def test_console_tab_shows_the_line_the_script_printed(runs, signed_in_page):
    """The console tab of a run.

    Expect: the console tab shows the line the example script printed."""
    _open(signed_in_page, _task_path(runs, "log"))
    signed_in_page.wait_for_selector(f"text={CONSOLE_LINE}", timeout=CHECK_TIMEOUT_MS)
    assert CONSOLE_LINE in signed_in_page.inner_text("body")


def test_debug_sample_renders_as_an_image(runs, signed_in_page):
    """The debug samples tab of a run.

    Expect: the debug samples tab renders the logo and the sample, both as images with a width."""
    _open(signed_in_page, _task_path(runs, "debugImages"))
    def rendered_images():
        widths = signed_in_page.evaluate("() => Array.from(document.images).map(i => i.naturalWidth)")
        return [w for w in widths if w > 0]

    assert _until(lambda: len(rendered_images()) >= 2, timeout=CHECK_TIMEOUT_MS / 1000), \
        f"the debug sample must render (logo and sample): {rendered_images()}"


def test_comparison_view_overlays_both_runs(runs, signed_in_page):
    """The project's experiments, in the scalars view: both runs are listed, and their metrics share one chart.

    Expect: both runs are listed in the scalars view, with the loss on one chart.
    """
    _open(signed_in_page, f"/projects/{runs['project_id']}/experiments")
    signed_in_page.wait_for_selector("text=e2e-web-run", timeout=CHECK_TIMEOUT_MS)
    signed_in_page.mouse.click(550, 92)  # the scalars view button of the experiments toolbar
    # the chart's labels are drawn after its frame: wait for the loss itself, not for the axis
    signed_in_page.wait_for_selector("text=loss", timeout=CHECK_TIMEOUT_MS)
    body = signed_in_page.inner_text("body")
    assert "Scalars" in body or "SCALARS" in body
    assert "loss" in body
    assert body.count("Iterations") >= 1
    names = [n for n in re.findall(r"e2e-web-run(?:-2)?-[0-9a-f]{6}", body)]
    assert len(set(names)) == 2, f"both runs must be listed in the comparison: {names}"


def test_project_tabs_offer_tasks_and_experiments(runs, signed_in_page):
    """The project header's tabs.

    Expect: the project header shows the TASKS and EXPERIMENTS tabs."""
    _open(signed_in_page, f"/projects/{runs['project_id']}/overview")
    signed_in_page.wait_for_selector("text=EXPERIMENTS", timeout=CHECK_TIMEOUT_MS)
    body = signed_in_page.inner_text("body")
    for tab in ("TASKS", "EXPERIMENTS"):
        assert tab in body, f"the project header must show the {tab} tab"


def test_task_folder_lists_its_experiments(runs, signed_in_page):
    """The project's TASKS tab lists the run's task (its name), as one folder with its experiments in it.

    Expect: the TASKS tab lists the run's task by its name.
    """
    _open(signed_in_page, f"/projects/{runs['project_id']}/tasks")
    assert _until(lambda: re.search(r"e2e-web-run-[0-9a-f]{6}", signed_in_page.inner_text("body")),
                  timeout=CHECK_TIMEOUT_MS / 1000), "the tasks tab must list the task of the run"


def test_single_click_selects_and_double_click_opens_a_project(runs, project, signed_in_page):
    """docs/e2e_testing/E2E.md: a single click on a project does not navigate; a double click opens its project dashboard.

    Expect: a single click keeps the dashboard, and a double click opens the project.
    """
    _open(signed_in_page, "/dashboard")
    card = signed_in_page.locator(f"text={project}").first
    card.wait_for(timeout=CHECK_TIMEOUT_MS)
    card.click()
    time.sleep(1)  # the negative check: a single click must not navigate, so there is nothing to wait for
    assert "/dashboard" in signed_in_page.url, "a single click must not navigate"
    card.dblclick()
    signed_in_page.wait_for_url(re.compile(rf"/projects/{runs['project_id']}"), timeout=CHECK_TIMEOUT_MS)


def test_workers_page_shows_a_running_job_without_a_reload(client_home, project, runs, signed_in_page, job_agent):
    """The Workers page, live: a job that is running.

    Expect: the worker running a job shows the job on the Workers page, without a reload of the page."""
    new_name_prefix = new_name("live")
    task_id = run_json(client_home, str(LIVE_JOB_SCRIPT), ["--project", project, "--name", new_name_prefix,
                                                           "--scalars", "1", "--sleep", "300"])["task_id"]
    api = workspace.device_api(client_home)
    jobs.enqueue(client_home, task_id)
    # the Workers tab of the workers and queues section: a worker lists the task it runs
    _open(signed_in_page, "/workers-and-queues/workers")
    # a job's first run starts its container cold (test_09, the completion test): the same longer wait applies
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        rows = signed_in_page.locator("tr, [role=row], mat-row").filter(has_text=new_name_prefix)
        if rows.count():
            return
        if jobs.task_record(api, task_id).get("status") in jobs.FINAL:
            break  # the job has ended without running: nothing more to see
        signed_in_page.wait_for_timeout(1000)
    # what the page shows when the job should have been running: a screenshot in the report folder, and the page text
    signed_in_page.screenshot(path=str(Path(__file__).parent / "reports" / "live-jobs-page.png"), full_page=True)
    body = signed_in_page.inner_text("body")
    pytest.fail(f"the job {new_name_prefix} never showed on the Workers page: {jobs.explain(api, task_id)}; "
                f"the page says: {body[:1500]!r}")
