"""Signing in to the web app in a headless browser, for the UI tests."""

import re

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import settings

DASHBOARD = re.compile(r"/dashboard")

# The "What's new" dialog opens for a user with release notes they have not seen, a second after the user and their
# preferences are loaded (web app, changes.service.ts, debounceTime(1000)), and its backdrop covers the page until it
# is closed. A user who has closed it once does not get it again, so it is waited for, not expected.
WHATS_NEW = "mat-dialog-container:has-text(\"What's new\")"
WHATS_NEW_WAIT_MS = 4_000


def sign_in(context, user=None, password=None):
    """Signs in through the identity provider's form, in a browser context of its own. Returns the page on the
    dashboard. The same form is the one cairnml init opens."""
    page = context.new_page()
    page.goto(settings.WEB_URL + "/")
    page.wait_for_selector("#username", timeout=60_000)
    page.fill("#username", user or settings.ADMIN_USER)
    page.fill("#password", password or settings.ADMIN_PASSWORD)
    page.click("#kc-login")
    page.wait_for_url(DASHBOARD, timeout=60_000)
    page.wait_for_selector("text=Recent tasks", timeout=60_000)
    dismiss_whats_new(page)
    return page


def dismiss_whats_new(page):
    """Closes the "What's new" dialog the way a person does, with its Dismiss button, if it opens. Waits for it up to
    WHATS_NEW_WAIT_MS (it returns at once when the dialog shows, and after that time when this user has seen the notes),
    then waits until it and its backdrop are gone, so the page can be clicked."""
    try:
        page.wait_for_selector(WHATS_NEW, timeout=WHATS_NEW_WAIT_MS)
    except PlaywrightTimeout:
        return
    page.click(f"{WHATS_NEW} >> text=Dismiss")
    page.wait_for_selector(".cdk-overlay-backdrop", state="detached", timeout=10_000)
