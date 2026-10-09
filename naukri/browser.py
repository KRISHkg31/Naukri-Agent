"""Chromium session handling: reuse the saved Naukri session, log in when it expires."""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from naukri.store import PlainStore

log = logging.getLogger(__name__)

PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
LOGIN_URL = "https://www.naukri.com/nlogin/login"


def user_agent(browser_version: str) -> str:
    """Normal desktop UA matching the real Chromium version and OS.

    Headless Chromium advertises "HeadlessChrome", which Naukri's Akamai edge blocks.
    """
    major = browser_version.split(".")[0]
    platform = "Windows NT 10.0; Win64; x64" if sys.platform == "win32" else "X11; Linux x86_64"
    return f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


OTP_INPUTS = (
    "input[id*='otp' i]:visible, input[name*='otp' i]:visible, "
    "input[placeholder*='otp' i]:visible, input[maxlength='1']:visible"
)


class LoginError(RuntimeError):
    pass


class NaukriBrowser:
    def __init__(self, store: "Path | PlainStore", headless: bool = True, slow_mo: int = 0, fresh: bool = False):
        """fresh=True ignores any saved session (start logged out); it is still saved to `store`."""
        self.store = PlainStore(store) if isinstance(store, Path) else store
        self.headless = headless
        self.slow_mo = slow_mo
        self.fresh = fresh

    def __enter__(self) -> "NaukriBrowser":
        self._pw = sync_playwright().start()
        # channel="chromium" = full browser in "new headless" mode; the default
        # headless shell gets "Access Denied" from Naukri.
        self.browser = self._pw.chromium.launch(
            headless=self.headless,
            channel="chromium",
            slow_mo=self.slow_mo,
            args=["--disable-blink-features=AutomationControlled"],
        )
        kwargs = dict(
            user_agent=user_agent(self.browser.version),
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        state = None if self.fresh else self.store.load()
        if state:
            kwargs["storage_state"] = state
        self.context = self.browser.new_context(**kwargs)
        self.context.set_default_timeout(20_000)
        self.page = self.context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()

    def save_session(self) -> None:
        self.store.save(self.context.storage_state())
        log.info("Session saved to %s", self.store)

    def open_profile(self) -> bool:
        """Load the profile page. Returns False if Naukri bounced us to login."""
        self.page.goto(PROFILE_URL, wait_until="domcontentloaded")
        try:
            self.page.get_by_text("Resume headline", exact=True).first.wait_for(timeout=20_000)
            self.page.wait_for_timeout(1500)  # let the lazy widgets finish rendering
            return True
        except PWTimeout:
            return False

    def ensure_logged_in(
        self,
        email: Optional[str],
        password: Optional[str],
        otp_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        if self.open_profile():
            log.info("Saved session is valid")
            return
        log.info("Session expired - logging in")
        if not email or not password:
            raise LoginError("Session expired and NAUKRI_EMAIL / NAUKRI_PASSWORD are not set")
        self.login(email, password, otp_provider)

    def login(self, email: str, password: str, otp_provider: Optional[Callable[[], str]] = None) -> None:
        page = self.page
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        field = page.locator("#usernameField")
        try:
            field.wait_for(state="visible", timeout=15_000)
        except PWTimeout:
            # A still-valid session makes Naukri redirect away from the login page.
            if "nlogin" not in page.url and self.open_profile():
                log.info("Already logged in")
                self.save_session()
                return
            raise LoginError("Naukri's login form did not load - try again in a few minutes")
        field.fill(email)
        page.locator("#passwordField").fill(password)
        page.locator("button[type='submit']").first.click()

        otp_done = False
        deadline = time.time() + 60
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            if "nlogin" not in page.url:
                break
            if page.locator("iframe[src*='recaptcha'], iframe[title*='captcha' i]").count():
                raise LoginError("Naukri showed a captcha - log in manually with `python main.py login`")
            errors = [t.strip() for t in page.locator(".commonErrorMsg:visible, .erLbl:visible").all_inner_texts()]
            if any(errors):
                raise LoginError("Naukri said: " + " ".join(e for e in errors if e))
            if not otp_done and page.locator(OTP_INPUTS).count():
                if otp_provider is None:
                    raise LoginError("Naukri asked for an OTP and no OTP provider is configured")
                self._enter_otp(otp_provider())
                otp_done = True
                deadline = time.time() + 60

        if not self.open_profile():
            raise LoginError(f"Login did not reach the profile page (stuck at {page.url})")
        self.save_session()
        log.info("Logged in")

    def _enter_otp(self, code: str) -> None:
        page = self.page
        boxes = page.locator("input[maxlength='1']:visible")
        if boxes.count() >= len(code):
            for i, digit in enumerate(code):
                boxes.nth(i).fill(digit)
        else:
            page.locator(OTP_INPUTS).first.fill(code)
        button = page.get_by_role("button", name=re.compile(r"verify|submit|login|continue", re.I)).first
        try:
            button.click(timeout=5_000)
        except PWTimeout:
            page.keyboard.press("Enter")
