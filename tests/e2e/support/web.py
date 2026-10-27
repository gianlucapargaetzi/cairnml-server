"""Signing in to the web app in a headless browser, for the UI tests."""

import re

from . import settings

DASHBOARD = re.compile(r"/dashboard")


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
    page.wait_for_selector("text=RECENT EXPERIMENTS", timeout=60_000)
    return page
