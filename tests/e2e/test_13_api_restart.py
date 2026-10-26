"""docs/e2e_testing/E2E.md, UI check after a restart of the API: this is the one test that restarts a service of the stack.

It is in its own module, which runs last (pytest runs the modules in name order), so no other test waits for the restart.
"""

import pytest

from support.web import sign_in

pytestmark = [pytest.mark.ui, pytest.mark.docker, pytest.mark.restart]


def test_restarting_the_api_keeps_the_dashboard_rendering(browser, isolated_stack):
    """Restart regression of docs/e2e_testing/E2E.md: after the API container restarts, a reload renders the same dashboard, with
    no half-loaded page. The restart is of this suite's own stack.

    Expect: after the API restarts and the page reloads, the same three dashboard sections are shown.
    """
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = sign_in(context)
    before = page.inner_text("body")
    result = isolated_stack.compose("restart", "apiserver", timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    isolated_stack._wait(isolated_stack._api_answers, 60, "the API after the restart")
    page.reload()
    page.wait_for_selector("text=RECENT EXPERIMENTS", timeout=60_000)
    after = page.inner_text("body")
    for heading in ("RECENT PROJECTS", "RECENT EXPERIMENTS", "MANAGE WORKERS AND QUEUES"):
        assert heading in after, f"{heading} missing after the restart"
    assert before.split("\n")[0] == after.split("\n")[0]
    context.close()
