"""Signs in at the identity provider's form in a headless browser.

The sign-in page is the one a person sees in their browser. cairnml-init opens it through the BROWSER
variable, which the tests set to this file (see cli.py). Run directly, it takes the sign-in address as
its argument and the account from E2E_LOGIN_USER and E2E_LOGIN_PASSWORD. It exits 0 once the sign-in
reply has reached the loopback listener ("Signed in" is shown), and non-zero otherwise.
"""

import os
import sys

SIGNED_IN = "Signed in"


def sign_in(url: str, username: str, password: str, timeout_ms: int = 60_000) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url)
            page.fill("#username", username)
            page.fill("#password", password)
            page.click("#kc-login")
            page.wait_for_selector(f"text={SIGNED_IN}", timeout=timeout_ms)
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        sign_in(sys.argv[1], os.environ["E2E_LOGIN_USER"], os.environ["E2E_LOGIN_PASSWORD"])
    except Exception as exc:  # the caller sees the failure through the exit code and its own timeout
        # the message tells the cause (a page that never showed, a refused connection); the password is never printed
        print(f"sign-in failed: {exc.__class__.__name__}: {str(exc).splitlines()[0] if str(exc) else ''}",
              file=sys.stderr)
        sys.exit(1)
