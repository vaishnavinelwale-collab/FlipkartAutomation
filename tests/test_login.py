"""Login tests. ALL OFFLINE: fake page, fake Gmail IMAP server, fake clock. They prove the decision
logic (missing credentials, field detection, OTP retrieval/failure, manual fallback, CAPTCHA pause,
success verification, failed authentication, secrets never leaking). They do NOT prove that Flipkart's
live site behaves this way - see the README / hand-over notes for the real-site check (--login-only)."""
import imaplib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402

import fk_login as L  # noqa: E402
from fk_common import DEFAULT_SETTINGS  # noqa: E402
from fk_login import (S_CAPTCHA, S_ERROR, S_ID, S_OTHER, S_OTP, S_PASSWORD, S_SIGNUP, Credentials,  # noqa: E402
                      OtpResult, run_login)

SETTINGS = {**DEFAULT_SETTINGS, "login_gmail_wait_seconds": 10, "login_manual_wait_seconds": 60,
            "login_step_timeout_seconds": 10}
OTP = "482913"
PW = "S3cret-pw-XYZ"
APP = "abcdefghijklmnop"
LOGIN_ID = "9876543210"
EMAIL_ID = "me@example.com"


# ----------------------------------------------------------------------------- fakes
class FakeClock:
    def __init__(self):
        self.t, self.hooks = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        for h in list(self.hooks):
            h(self.t)

    def utcnow(self):
        return datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=self.t)


class FakePage:
    """Implements the page interface that run_login() uses."""

    def __init__(self, initial=S_ID, after_submit=S_OTP, after_verify=S_OTHER, verified=True,
                 has_field=True, has_button=True, fill_ok=True):
        self._state, self.after_submit, self.after_verify = initial, after_submit, after_verify
        self.verified, self.has_field, self.has_button, self.fill_ok = verified, has_field, has_button, fill_ok
        self.typed, self.otp_entered, self.pw_entered, self.verify_calls, self.snapshots = None, None, None, 0, 0
        self.resend_clicks, self.resend_available = 0, False

    def open_login(self):
        pass

    def state(self):
        return self._state

    def type_id(self, text):
        self.typed = text
        return self.has_field

    def click_submit(self):
        if self.has_button:
            self._state = self.after_submit
        return self.has_button

    def fill_otp(self, otp):
        self.otp_entered = otp
        return self.fill_ok

    def fill_password(self, pw):
        self.pw_entered = pw
        return self.fill_ok

    def click_verify(self):
        self._state = self.after_verify
        return True

    def click_resend_otp(self):
        if self.resend_available:
            self.resend_clicks += 1
            return True
        return False

    def account_verified(self):
        self.verify_calls += 1
        return self.verified

    def current_path(self):
        return "https://www.flipkart.com/account/login"

    def snapshot(self, tag):          # login must never take screenshots / HTML dumps
        self.snapshots += 1


def creds(**kw):
    base = dict(login_id=EMAIL_ID, gmail_user="me@gmail.com", gmail_app_password=APP, password=None)
    base.update(kw)
    return Credentials(**base)


def run(page, c=None, interactive=True, fetch=None, clock=None, settings=None):
    announced = []
    res = run_login(page, c or creds(), settings or SETTINGS, interactive=interactive,
                    clock=clock or FakeClock(), fetch_otp=fetch or (lambda *a: OtpResult(otp=OTP)),
                    announce=lambda msg, *a: announced.append(msg % a if a else msg))
    return res, announced


NO_OTP = lambda *a, **kw: OtpResult(reason="no matching OTP/SMS arrived in time")  # noqa: E731


# ----------------------------------------------------------------------------- credentials
def test_missing_credentials_means_not_configured_and_driver_untouched():
    res = L.login_to_flipkart(None, SETTINGS, creds=Credentials())        # driver=None: any use would crash
    assert res.status == L.NOT_CONFIGURED and not res.logged_in and "FKART_LOGIN_ID" in res.reason


def test_login_disabled_in_config():
    res = L.login_to_flipkart(None, {**SETTINGS, "login_enabled": False}, creds=creds())
    assert res.status == L.DISABLED


def test_env_placeholders_and_blank_values_are_ignored(monkeypatch):
    for k, v in {"FKART_LOGIN_ID": "your_flipkart_email_or_mobile_number", "FKART_EMAIL": "your_gmail_address@gmail.com",
                 "FKART_APP_PASSWORD": "your_gmail_app_password", "FKART_PASSWORD": ""}.items():
        monkeypatch.setenv(k, v)
    c = L.load_credentials()
    assert c.login_id is None and not c.gmail_ready and c.password is None


def test_env_real_values_and_app_password_spaces(monkeypatch):
    monkeypatch.setenv("FKART_LOGIN_ID", "  me@example.com ")
    monkeypatch.setenv("FKART_EMAIL", "inbox@gmail.com")
    monkeypatch.setenv("FKART_APP_PASSWORD", "abcd efgh ijkl mnop")
    c = L.load_credentials()
    assert c.login_id == "me@example.com" and c.gmail_user == "inbox@gmail.com"
    assert c.gmail_app_password == "abcdefghijklmnop" and c.gmail_ready


def test_legacy_env_falls_back_to_fkart_email_as_login_id(monkeypatch):
    monkeypatch.delenv("FKART_LOGIN_ID", raising=False)
    monkeypatch.setenv("FKART_EMAIL", "old@gmail.com")
    c = L.load_credentials()
    assert c.login_id == "old@gmail.com" and c.login_id_from_legacy


def test_credentials_repr_hides_secrets():
    r = repr(creds(password=PW))
    assert PW not in r and APP not in r


def test_mask_and_redact():
    assert L.mask_identifier("someone@gmail.com") == "so***@gmail.com"
    assert L.mask_identifier("9876543210") == "98******10"
    assert L.redact(f"bad {PW} here", [PW]) == "bad *** here"
    assert L.is_phone("+91 98765-43210") and not L.is_phone("a@b.com")


def test_dotenv_fallback_reader_handles_bom_and_quotes(tmp_path, monkeypatch):
    import fk_common
    f = tmp_path / ".env"
    f.write_bytes("\ufeffFKART_LOGIN_ID=\"abc@x.com\"\n# c\nFKART_PASSWORD='p w'\n".encode("utf-8"))
    monkeypatch.delenv("FKART_LOGIN_ID", raising=False)
    monkeypatch.delenv("FKART_PASSWORD", raising=False)
    fk_common._load_env_file(f)
    assert os.environ["FKART_LOGIN_ID"] == "abc@x.com" and os.environ["FKART_PASSWORD"] == "p w"


# ----------------------------------------------------------------------------- field detection
def inp(**kw):
    return {"type": "text", "visible": True, **kw}


def test_login_field_ignores_header_search_box():
    search = inp(name="q", placeholder="Search for products, brands and more")
    mobile = inp(placeholder="Enter Email/Mobile number")
    assert L.pick_login_field([search, mobile]) is mobile


def test_login_field_none_when_only_search_or_hidden():
    assert L.pick_login_field([inp(name="q", placeholder="Search")]) is None
    assert L.pick_login_field([inp(placeholder="Mobile", visible=False)]) is None
    assert L.pick_login_field([]) is None


def test_login_field_single_unlabelled_input_is_accepted_but_ambiguous_pair_is_not():
    only = inp()
    assert L.pick_login_field([only]) is only
    assert L.pick_login_field([inp(), inp()]) is None


def test_login_field_skips_password_and_otp_boxes():
    boxes = [inp(maxlength="1") for _ in range(6)]
    assert L.pick_login_field([inp(type="password"), *boxes]) is None


def test_otp_inputs_six_boxes_or_single_hinted():
    boxes = [inp(maxlength="1") for _ in range(6)]
    assert L.pick_otp_inputs([inp(placeholder="Mobile"), *boxes]) == boxes
    one = inp(placeholder="Enter OTP")
    assert L.pick_otp_inputs([inp(placeholder="Mobile"), one]) == [one]


# ----------------------------------------------------------------------------- page-state classification
@pytest.mark.parametrize("text,inputs,expected", [
    ("Login Enter Email/Mobile number Request OTP", [inp(placeholder="Enter Email/Mobile number")], S_ID),
    ("Enter OTP sent to 98****10 Resend", [inp(placeholder="Enter OTP")], S_OTP),
    ("verify", [inp(maxlength="1") for _ in range(6)], S_OTP),
    ("Enter your password", [inp(type="password")], S_PASSWORD),
    ("Please complete the CAPTCHA", [], S_CAPTCHA),
    ("We detected unusual traffic from your network", [], S_CAPTCHA),
    ("Incorrect OTP. Please try again", [inp(placeholder="Enter OTP")], S_ERROR),
    ("Looks like you're new here! Create an account", [], S_SIGNUP),
    ("Welcome back", [inp(name="q", placeholder="Search")], S_OTHER),
])
def test_classify_login_state(text, inputs, expected):
    assert L.classify_login_state(text, inputs) == expected


MARKERS = DEFAULT_SETTINGS["login_success_markers"]


def test_account_verification_requires_non_login_page_and_marker():
    ok = "Profile Information  Manage Addresses  Logout"
    assert L.verify_account_state("https://www.flipkart.com/account/?rd=0", ok, [inp(name="q", placeholder="Search")], MARKERS)
    assert not L.verify_account_state("https://www.flipkart.com/account/login?ret=/account", ok, [], MARKERS)   # redirected
    assert not L.verify_account_state("https://www.flipkart.com/account/", "Enter Email/Mobile number Request OTP", [], MARKERS)
    assert not L.verify_account_state("https://www.flipkart.com/account/", "Home page, nothing about the account", [], MARKERS)


# ----------------------------------------------------------------------------- OTP e-mail extraction
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def mail(frm="Flipkart <noreply@flipkart.com>", subject="Flipkart login OTP", body=f"Your OTP is {OTP}. Do not share.",
         when=NOW + timedelta(seconds=20), html=False, date=True):
    m = EmailMessage()
    m["From"], m["Subject"] = frm, subject
    if date:
        m["Date"] = format_datetime(when)
    if html:
        m.set_content("fallback")
        m.clear_content()
        m.set_content(f"<html><body><p>Your <b>OTP</b> is <b>{OTP}</b></p></body></html>", subtype="html")
    else:
        m.set_content(body)
    return m


def test_extract_otp_plain_and_html_only():
    assert L.extract_otp(mail(), NOW) == OTP
    assert L.extract_otp(mail(html=True), NOW) == OTP


def test_extract_otp_rejects_wrong_sender_stale_undated_and_non_otp():
    assert L.extract_otp(mail(frm="Phisher <otp@flipkart.com.evil.io>"), NOW) is None
    assert L.extract_otp(mail(frm="x@notflipkart.com"), NOW) is None
    assert L.extract_otp(mail(when=NOW - timedelta(minutes=10)), NOW) is None      # old OTP
    assert L.extract_otp(mail(date=False), NOW) is None                            # unparseable date
    assert L.extract_otp(mail(subject="Your order 123456 shipped", body="Order 123456 is on its way"), NOW) is None


def test_extract_otp_accepts_flipkart_subdomain():
    assert L.extract_otp(mail(frm="no-reply@mailer.flipkart.com"), NOW) == OTP


class FakeIMAP:
    """imaplib.IMAP4_SSL stand-in. `script` is a list of per-connection behaviours."""
    connections = 0

    def __init__(self, messages=None, login_error=None, connect_fail=0):
        self.messages, self.login_error, self.connect_fail = messages or [], login_error, connect_fail
        self.readonly, self.logged_out = None, 0

    def factory(self, host):
        FakeIMAP.connections += 1
        if FakeIMAP.connections <= self.connect_fail:
            raise OSError("network down")
        return self

    def login(self, u, p):
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)

    def select(self, box, readonly=False):
        self.readonly = readonly

    def search(self, charset, crit):
        self.criteria = crit
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, mid, spec):
        return "OK", [(b"x", self.messages[int(mid) - 1].as_bytes())]

    def logout(self):
        self.logged_out += 1


def fetch(imap, **kw):
    FakeIMAP.connections = 0
    return L.fetch_otp_from_gmail("me@gmail.com", APP, NOW, timeout=kw.pop("timeout", 20), interval=5,
                                  imap_factory=imap.factory, clock=kw.pop("clock", FakeClock()), **kw)


def test_gmail_fetch_success_is_read_only_and_picks_newest():
    older = mail(body="Your OTP is 111111", when=NOW + timedelta(seconds=5))
    newer = mail(when=NOW + timedelta(seconds=40))
    imap = FakeIMAP([older, newer])
    r = fetch(imap)
    assert r.otp == OTP and imap.readonly is True and imap.logged_out >= 1
    assert "FROM \"flipkart\"" in imap.criteria and "SINCE 20-Oct-2026" in imap.criteria


def test_gmail_fetch_times_out_with_clear_reason():
    r = fetch(FakeIMAP([mail(when=NOW - timedelta(hours=1))]))
    assert r.otp is None and "no matching OTP" in r.reason


def test_gmail_auth_failure_stops_immediately_and_never_leaks_password():
    clock = FakeClock()
    r = fetch(FakeIMAP(login_error=f"[AUTHENTICATIONFAILED] {APP}"), clock=clock)
    assert r.otp is None and APP not in r.reason and "rejected" in r.reason
    assert clock.t == 0                                           # no pointless retries


def test_gmail_network_error_is_retried():
    r = fetch(FakeIMAP([mail()], connect_fail=2))
    assert r.otp == OTP


def test_load_credentials_accepts_phone_number(monkeypatch):
    values = {"FKART_LOGIN_ID": "+91 98765 43210", "FKART_EMAIL": "", "FKART_APP_PASSWORD": "", "FKART_PASSWORD": ""}
    monkeypatch.setattr(L, "env", lambda key, default=None: values.get(key, default))
    assert L.load_credentials().login_id == "+91 98765 43210"


# ----------------------------------------------------------------------------- run_login flows
def test_otp_from_gmail_success_verifies_account_before_reporting_login():
    page = FakePage()
    res, _ = run(page, c=creds(login_id="me@example.com"))
    assert res.status == L.LOGGED_IN and res.method == "otp_gmail" and res.logged_in
    assert page.typed == "me@example.com" and page.otp_entered == OTP and page.verify_calls >= 1
    assert res.manual_steps == [] and res.reason == ""


def test_gmail_otp_failure_falls_back_to_manual_entry_in_visible_browser():
    page, clock = FakePage(), FakeClock()

    def human(t):                      # the person types the OTP and presses Verify after 20 s
        if t >= 20 and page.state() == S_OTP:
            page._state = S_OTHER
    clock.hooks.append(human)
    res, announced = run(page, c=creds(login_id="me@example.com"), fetch=NO_OTP, clock=clock)
    assert res.status == L.LOGGED_IN and res.method == "otp_manual"
    assert res.manual_steps and any("ACTION REQUIRED" in a for a in announced)
    assert page.otp_entered is None                                 # script did not type anything


def test_manual_otp_not_completed_in_time_fails():
    res, _ = run(FakePage(), c=creds(login_id="me@example.com"), fetch=NO_OTP)
    assert res.status == L.FAILED and not res.logged_in and "OTP" in res.reason


def test_headless_cannot_do_manual_otp_fails_fast():
    clock = FakeClock()
    res, announced = run(FakePage(), c=creds(login_id="me@example.com"), fetch=NO_OTP, interactive=False, clock=clock)
    assert res.status == L.FAILED and "headless" in res.reason and not announced
    assert clock.t < 30


def test_no_gmail_configured_goes_straight_to_manual():
    called = []
    res, announced = run(FakePage(), c=creds(login_id=LOGIN_ID, gmail_user=None, gmail_app_password=None),
                         fetch=lambda *a: called.append(1) or OtpResult(otp=OTP))
    assert not called and res.status == L.FAILED and announced        # asked the human, nobody answered


def test_phone_login_waits_for_verified_account_after_call_prompt():
    page, clock = FakePage(), FakeClock()
    page.account_verified_current = lambda: clock.t >= 10
    res, announced = run(
        page,
        clock=clock,
        settings={**SETTINGS, "login_phone_confirmation_wait_seconds": 30},
    )
    assert res.status == L.LOGGED_IN and res.method == "otp_gmail"
    assert clock.t >= 10
    assert any("calls your phone for verification" in msg for msg in announced)


def test_phone_login_id_is_not_read_automatically_and_headless_fails_fast():
    called = []
    res, _ = run(FakePage(), c=creds(login_id=LOGIN_ID), interactive=False,
                 fetch=lambda *a: called.append(1) or OtpResult(otp=OTP))
    assert not called and res.status == L.FAILED and not res.logged_in


def test_phone_login_id_asks_the_person_to_type_the_otp():
    page, clock = FakePage(), FakeClock()
    clock.hooks.append(lambda t: setattr(page, "_state", S_OTHER) if t >= 10 else None)
    res, announced = run(page, c=creds(login_id=LOGIN_ID), clock=clock)
    assert res.status == L.LOGGED_IN and res.method == "otp_manual" and announced


def test_wrong_otp_is_reported_as_failure_not_success():
    page = FakePage(after_verify=S_ERROR)
    res, _ = run(page)
    assert res.status == L.FAILED and "rejected the OTP" in res.reason and not res.logged_in


def test_otp_step_still_showing_after_submit_is_a_failure():
    page = FakePage(after_verify=S_OTP)
    res, _ = run(page)
    assert res.status == L.FAILED and "still shows the OTP" in res.reason


def test_login_not_reported_when_account_page_cannot_be_verified():
    page = FakePage(verified=False)
    res, _ = run(page)
    assert res.status == L.FAILED and "sign-in was not confirmed" in res.reason and page.verify_calls >= 1


def test_captcha_after_submit_pauses_for_user_then_continues():
    page, clock = FakePage(after_submit=S_CAPTCHA), FakeClock()

    def human(t):
        if t >= 15 and page.state() == S_CAPTCHA:
            page._state = S_OTP
    clock.hooks.append(human)
    res, announced = run(page, clock=clock)
    assert res.status == L.LOGGED_IN and any("CAPTCHA" in a for a in announced)
    assert "complete the security verification / CAPTCHA" in res.manual_steps


def test_captcha_never_completed_fails_and_nothing_is_bypassed():
    page = FakePage(after_submit=S_CAPTCHA)
    res, _ = run(page)
    assert res.status == L.FAILED and "not completed" in res.reason
    assert page.otp_entered is None and page.pw_entered is None


def test_captcha_in_headless_fails_fast():
    res, _ = run(FakePage(initial=S_CAPTCHA), interactive=False)
    assert res.status == L.FAILED and "headless" in res.reason


def test_password_step_uses_fkart_password_when_set():
    page = FakePage(after_submit=S_PASSWORD)
    res, _ = run(page, c=creds(password=PW))
    assert res.status == L.LOGGED_IN and res.method == "password" and page.pw_entered == PW


def test_password_step_without_password_waits_for_user():
    page, clock = FakePage(after_submit=S_PASSWORD), FakeClock()
    clock.hooks.append(lambda t: setattr(page, "_state", S_OTHER) if t >= 10 else None)
    res, announced = run(page, clock=clock)
    assert res.status == L.LOGGED_IN and res.method == "password_manual" and page.pw_entered is None
    assert announced


def test_password_not_used_when_flipkart_asks_for_otp():
    page = FakePage(after_submit=S_OTP)
    res, _ = run(page, c=creds(password=PW))
    assert page.pw_entered is None and res.method == "otp_gmail"


def test_unknown_account_signup_page_fails_and_creates_nothing():
    page = FakePage(after_submit=S_SIGNUP)
    res, _ = run(page)
    assert res.status == L.FAILED and "does not recognise" in res.reason and page.otp_entered is None


def test_error_message_after_submit_fails():
    res, _ = run(FakePage(after_submit=S_ERROR))
    assert res.status == L.FAILED and res.state == S_ERROR


def test_login_field_not_found_fails_cleanly():
    res, _ = run(FakePage(initial=S_OTHER, verified=False))
    assert res.status == L.FAILED and "login field not found" in res.reason
    res, _ = run(FakePage(has_field=False))
    assert res.status == L.FAILED and "could not type" in res.reason


def test_fresh_login_does_not_accept_existing_saved_session():
    res, _ = run(FakePage(initial=S_OTHER, verified=True),
                 settings={**SETTINGS, "_require_login_challenge": True})
    assert res.status == L.FAILED and "fresh login was requested" in res.reason
    assert res.method == "none"


def test_submit_button_missing_fails():
    res, _ = run(FakePage(has_button=False))
    assert res.status == L.FAILED and "button was not found" in res.reason


def test_otp_found_but_cannot_be_entered_fails():
    res, _ = run(FakePage(fill_ok=False))
    assert res.status == L.FAILED and "could not be entered" in res.reason


def test_secrets_never_appear_in_logs_results_or_snapshots(caplog):
    caplog.set_level(logging.DEBUG, logger="fk")
    for page, kw in [(FakePage(after_verify=S_ERROR), {}), (FakePage(after_submit=S_PASSWORD, after_verify=S_ERROR), {}),
                     (FakePage(), {}), (FakePage(), {"fetch": NO_OTP})]:
        res, announced = run(page, c=creds(password=PW), **kw)
        blob = json.dumps(res.as_dict()) + " ".join(announced)
        for secret in (PW, APP, OTP):
            assert secret not in blob
        assert page.snapshots == 0                             # no screenshots / HTML dumps during login
    for secret in (PW, APP, OTP):
        assert secret not in caplog.text


def test_result_dict_is_report_ready_and_masks_login_id():
    res, _ = run(FakePage())
    d = res.as_dict()
    assert d["logged_in"] is True and d["login_id_masked"] == "me***@example.com" and EMAIL_ID not in json.dumps(d)


def test_unexpected_exception_becomes_failed_result_with_redacted_text():
    class Boom(FakePage):
        def open_login(self):
            raise RuntimeError(f"driver exploded with {PW}")
    orig = L.LiveLoginPage
    L.LiveLoginPage = lambda driver, settings: Boom()
    try:
        res = L.login_to_flipkart(object(), SETTINGS, creds=creds(password=PW))
    finally:
        L.LiveLoginPage = orig
    assert res.status == L.FAILED and PW not in res.reason and "RuntimeError" in res.reason


# ----------------------------------------------------------------------------- Selenium adapter against a fake driver
class FakeEl:
    def __init__(self, tag="input", text="", displayed=True, on_click=None, **attrs):
        self.tag_name, self.text, self._shown, self.attrs = tag, text, displayed, attrs
        self.sent, self.clicked, self.on_click = [], 0, on_click

    def is_displayed(self):
        return self._shown

    def get_attribute(self, k):
        return self.attrs.get(k)

    def send_keys(self, *a):
        self.sent += list(a)

    def clear(self):
        self.sent = []

    def click(self):
        self.clicked += 1
        if self.on_click:
            self.on_click()


class FakeDriver:
    PAD = " lorem ipsum" * 30                                  # > 200 chars so wait_for_content() returns at once

    def __init__(self, inputs, clickables, text, redirect_account_to_login=False,
                 authenticated_account_menu=False):
        self.inputs, self.clickables, self.text = inputs, clickables, text + self.PAD
        self.current_url, self.visited, self.redirect = "https://www.flipkart.com/account/login", [], redirect_account_to_login
        self.authenticated_account_menu = authenticated_account_menu

    def get(self, url):
        self.visited.append(url)
        self.current_url = "https://www.flipkart.com/account/login?ret=%2Faccount" if self.redirect and "/account" in url else url

    def find_elements(self, by, value):
        return self.inputs if by == By.TAG_NAME else self.clickables

    def execute_script(self, script, *a):
        if "flipkart-account-menu-probe" in script:
            return self.authenticated_account_menu
        return self.text


def login_page_driver():
    search = FakeEl(name="q", placeholder="Search for products", type="text")
    mobile = FakeEl(placeholder="Enter Email/Mobile number", type="text")
    header_login = FakeEl("a", "Login")
    request = FakeEl("button", "Request OTP")
    return FakeDriver([search, mobile], [header_login, request], "Login Enter Email/Mobile number Request OTP"), search, mobile, header_login, request


def test_open_login_clicks_homepage_login_when_login_url_redirects_home(monkeypatch):
    import fk_browser

    search = FakeEl(name="q", placeholder="Search for Products", type="text")
    mobile = FakeEl(placeholder="Enter Email/Mobile number", type="text")
    drv = FakeDriver([search], [], "Search for Products")

    def open_dialog():
        drv.inputs = [search, mobile]
        drv.clickables = [FakeEl("button", "Request OTP")]
        drv.text = "Login Enter Email/Mobile number Request OTP" + drv.PAD

    login = FakeEl("a", "Login", on_click=open_dialog)
    drv.clickables = [login]
    monkeypatch.setattr(fk_browser, "safe_get", lambda driver, url, retries, wait: driver.get(url) or True)
    page = L.LiveLoginPage(drv, SETTINGS)
    page.open_login()
    assert login.clicked == 1
    assert page.state() == S_ID
    assert drv.visited == [SETTINGS["login_url"]]


def test_live_page_types_into_right_field_and_clicks_request_otp_not_header_login():
    drv, search, mobile, header_login, request = login_page_driver()
    page = L.LiveLoginPage(drv, SETTINGS)
    assert page.state() == S_ID
    assert page.type_id(LOGIN_ID) and mobile.sent == [LOGIN_ID] and search.sent == []
    assert page.click_submit() and request.clicked == 1 and header_login.clicked == 0


@pytest.mark.parametrize(
    "loaded,body,title,expected",
    [
        (True, "Ratings and Reviews", "Product Reviews", "loaded_empty"),
        (False, "Access denied", "Access Denied", "blocked"),
        (False, "", "", "navigation_or_content_timeout"),
    ],
)
def test_live_page_pagination_diagnostic_distinguishes_empty_blocked_and_timeout(
        monkeypatch, tmp_path, loaded, body, title, expected):
    import fk_browser

    drv = FakeDriver([], [], body + FakeDriver.PAD if body else "")
    drv.title = title
    def fake_safe_get(driver, url, *args, **kwargs):
        driver.current_url = url
        return loaded

    monkeypatch.setattr(fk_browser, "safe_get", fake_safe_get)
    page = fk_browser.LivePage(drv, {**SETTINGS, "page_wait_seconds": 1}, tmp_path)
    page.wait_for_review_entries = lambda timeout: 0

    assert page.goto_page("https://www.flipkart.com/product-reviews/item?page=11") is False
    assert page.last_page_navigation["status"] == expected
    assert page.last_page_navigation["review_blocks"] == 0


def test_live_page_submit_falls_back_to_enter_key():
    drv, _, mobile, _, request = login_page_driver()
    drv.clickables = []
    page = L.LiveLoginPage(drv, SETTINGS)
    page.type_id(LOGIN_ID)
    assert page.click_submit() and L.Keys.RETURN in mobile.sent


def test_live_page_fills_six_otp_boxes_one_digit_each():
    boxes = [FakeEl(maxlength="1", type="text") for _ in range(6)]
    drv = FakeDriver(boxes, [FakeEl("button", "Verify")], "Enter OTP sent to 98****10 Resend")
    page = L.LiveLoginPage(drv, SETTINGS)
    assert page.state() == S_OTP and page.fill_otp(OTP)
    assert ["".join(b.sent) for b in boxes] == list(OTP)
    assert page.click_verify() and drv.clickables[0].clicked == 1


def test_live_page_clicks_enabled_resend_once():
    resend = FakeEl("button", "Resend OTP")
    drv = FakeDriver([], [resend], "Enter OTP")
    page = L.LiveLoginPage(drv, {**SETTINGS, "login_resend_wait_seconds": 0})
    assert page.click_resend_otp() and resend.clicked == 1


def test_live_page_does_not_click_disabled_resend():
    resend = FakeEl("button", "Resend OTP", disabled="true")
    drv = FakeDriver([], [resend], "Enter OTP")
    page = L.LiveLoginPage(drv, {**SETTINGS, "login_resend_wait_seconds": 0})
    assert not page.click_resend_otp() and resend.clicked == 0


def test_live_page_account_verification_false_when_redirected_to_login():
    drv, *_ = login_page_driver()
    drv.redirect = True
    assert L.LiveLoginPage(drv, SETTINGS).account_verified() is False


def test_live_page_account_verification_true_on_real_account_page():
    drv = FakeDriver([FakeEl(name="q", placeholder="Search", type="text")], [], "Profile Information Manage Addresses Logout")
    page = L.LiveLoginPage(drv, SETTINGS)
    assert page.account_verified() is True and SETTINGS["login_verify_url"] in drv.visited


def test_live_page_accepts_signed_in_homepage_account_name_dropdown():
    search = FakeEl(name="q", placeholder="Search for products", type="text")
    drv = FakeDriver([search], [], "Search for products", authenticated_account_menu=True)
    drv.current_url = "https://www.flipkart.com/"
    assert L.LiveLoginPage(drv, SETTINGS).account_verified_current() is True


def test_account_dropdown_does_not_override_login_page_or_otp_form():
    login, *_ = login_page_driver()
    login.authenticated_account_menu = True
    assert L.LiveLoginPage(login, SETTINGS).account_verified_current() is False


def test_current_path_drops_query_string():
    drv, *_ = login_page_driver()
    drv.current_url = "https://www.flipkart.com/account/login?token=abc123&otp=482913"
    assert L.LiveLoginPage(drv, SETTINGS).current_path() == "https://www.flipkart.com/account/login"


# ----------------------------------------------------------------------------- integration with the scraper main()
class _Drv:
    def __init__(self, dead=False):
        self.dead, self.quit_called = dead, False

    @property
    def title(self):
        if self.dead:
            raise RuntimeError("dead browser")
        return "t"

    def quit(self):
        self.quit_called = True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import fk_browser
    import Flipkart_Reviews_Hearables as main
    log_, drivers, logins = [], [], []
    settings = {**DEFAULT_SETTINGS, "headless": False, "login_required": False,
                "output_dir": str(tmp_path / "out"), "diagnostics_dir": str(tmp_path / "diag")}
    monkeypatch.setattr(main, "load_config", lambda: (settings, {}))
    monkeypatch.setattr(main, "load_products", lambda p: [("https://x/p1", "P1"), ("https://x/p2", "P2")])
    monkeypatch.setattr(main, "REPORT_PATH", tmp_path / "run_report.json")
    monkeypatch.setattr(main, "send_alert", lambda results: None)

    def fake_driver(headless=False, profile_dir=None):
        d = _Drv()
        d.profile_dir = profile_dir
        drivers.append(d)
        log_.append("driver")
        return d
    monkeypatch.setattr(fk_browser, "make_driver", fake_driver)

    outcome = {"result": L.LoginResult(status=L.LOGGED_IN, method="otp_gmail", login_id_masked="98******10")}

    def fake_login(driver, st, **kw):
        log_.append("login")
        logins.append(driver)
        return outcome["result"]
    monkeypatch.setattr(L, "login_to_flipkart", fake_login)

    def fake_process(driver, url, name, st, sel, dirs):
        log_.append(f"scrape:{name}")
        return {"product": name, "status": "success", "failure_reason": "", "warnings": []}
    monkeypatch.setattr(main, "process_product", fake_process)
    return main, settings, outcome, log_, drivers, logins, tmp_path / "run_report.json"


def test_main_logs_in_before_scraping_and_reuses_session(harness):
    main, settings, outcome, log_, drivers, logins, report = harness
    assert main.main([]) == 0
    assert log_ == ["driver", "login", "scrape:P1", "scrape:P2"]      # login strictly first, one browser
    assert drivers[0].profile_dir == settings["chrome_profile_dir"]
    assert len(drivers) == 1 and drivers[0].quit_called
    data = json.loads(report.read_text())
    assert data["login"]["status"] == "logged_in" and len(data["results"]) == 2


def test_main_optional_login_failure_continues_and_reports_it(harness):
    main, settings, outcome, log_, drivers, logins, report = harness
    outcome["result"] = L.LoginResult(status=L.FAILED, reason="OTP not completed in time")
    assert main.main([]) == 0
    assert log_.count("login") == 1 and "scrape:P1" in log_ and "scrape:P2" in log_
    data = json.loads(report.read_text())
    assert data["login"]["status"] == "failed" and data["login"]["logged_in"] is False
    assert "OTP not completed" in data["login"]["reason"]


def test_main_login_required_failure_stops_before_any_product(harness):
    main, settings, outcome, log_, drivers, logins, report = harness
    settings["login_required"] = True
    outcome["result"] = L.LoginResult(status=L.FAILED, reason="CAPTCHA not completed")
    assert main.main([]) == 1
    assert not any(x.startswith("scrape") for x in log_) and drivers[0].quit_called
    assert json.loads(report.read_text())["login"]["status"] == "failed"


def test_main_not_configured_login_is_reported_not_hidden(harness):
    main, settings, outcome, log_, drivers, logins, report = harness
    outcome["result"] = L.LoginResult(status=L.NOT_CONFIGURED, reason="FKART_LOGIN_ID is not set")
    assert main.main([]) == 0
    assert json.loads(report.read_text())["login"]["status"] == "not_configured"


def test_main_login_only_mode_exit_codes_and_no_scraping(harness):
    main, settings, outcome, log_, drivers, logins, report = harness
    assert main.main(["--login-only"]) == 0
    data = json.loads(report.read_text())
    assert data["login"]["status"] == L.LOGGED_IN
    assert data["login"]["method"] == "otp_gmail"
    assert data["results"] == []
    outcome["result"] = L.LoginResult(status=L.FAILED, reason="x")
    assert main.main(["--login-only"]) == 1
    assert json.loads(report.read_text())["login"]["status"] == L.FAILED
    assert not any(x.startswith("scrape") for x in log_)


def test_main_fresh_login_clears_flipkart_session_before_login(harness, monkeypatch):
    main, settings, _, log_, drivers, _, _ = harness
    normal_profile = settings["chrome_profile_dir"]
    monkeypatch.setattr(main, "clear_flipkart_session", lambda driver: log_.append("clear_flipkart_session"))
    assert main.main(["--show-browser", "--fresh-login"]) == 0
    assert log_ == ["driver", "clear_flipkart_session", "login", "scrape:P1", "scrape:P2"]
    assert settings["_require_login_challenge"] is True
    assert drivers[0].profile_dir == main.fresh_login_profile_dir(normal_profile)
    assert drivers[0].profile_dir != normal_profile
    assert settings["chrome_profile_dir"] == normal_profile


def test_main_fresh_login_scrapes_one_validated_product_url(harness, monkeypatch):
    main, _, _, log_, _, _, _ = harness
    import product_resolver
    product_url = "https://www.flipkart.com/audio/p/itm123"
    monkeypatch.setattr(main, "clear_flipkart_session", lambda driver: log_.append("clear_flipkart_session"))

    def resolve(driver, url, timeout):
        log_.append("resolve_product")
        assert url == product_url
        return "Chosen product", url

    monkeypatch.setattr(product_resolver, "resolve_product", resolve)
    assert main.main(["--show-browser", "--fresh-login", "--product-url", product_url]) == 0
    assert log_ == ["driver", "clear_flipkart_session", "login", "resolve_product", "scrape:Chosen product"]


def test_main_single_product_url_rejects_non_flipkart_url(harness):
    main, _, _, log_, _, _, _ = harness
    assert main.main(["--product-url", "https://example.com/item"]) == 1
    assert log_ == []


def test_main_missing_bulk_csv_explains_single_product_option(harness, monkeypatch, caplog):
    main, _, _, log_, _, _, _ = harness
    monkeypatch.setattr(main, "load_products", lambda path: (_ for _ in ()).throw(FileNotFoundError(path)))
    assert main.main([]) == 1
    assert "--product-url" in caplog.text and "restore the configured CSV" in caplog.text
    assert log_ == []


def test_main_fresh_login_only_does_not_scrape(harness, monkeypatch):
    main, _, _, log_, _, _, _ = harness
    monkeypatch.setattr(main, "clear_flipkart_session", lambda driver: log_.append("clear_flipkart_session"))
    assert main.main(["--login-only", "--show-browser", "--fresh-login"]) == 0
    assert log_ == ["driver", "clear_flipkart_session", "login"]


def test_main_fresh_login_failure_never_scrapes_even_if_login_is_optional(harness, monkeypatch):
    main, settings, outcome, log_, _, _, report = harness
    settings["login_required"] = False
    outcome["result"] = L.LoginResult(status=L.FAILED, reason="fresh login not completed")
    monkeypatch.setattr(main, "clear_flipkart_session", lambda driver: log_.append("clear_flipkart_session"))
    assert main.main(["--show-browser", "--fresh-login"]) == 1
    assert not any(x.startswith("scrape:") for x in log_)
    assert json.loads(report.read_text())["results"] == []


def test_clear_flipkart_session_clears_only_flipkart_origins(harness):
    main, *_ = harness

    class Driver:
        def __init__(self):
            self.cleared, self.visited, self.scripts = [], [], []
        def get(self, url):
            self.visited.append(url)
        def delete_all_cookies(self):
            self.cleared.append("cookies for current Flipkart origin")
        def execute_script(self, script):
            self.scripts.append(script)

    driver = Driver()
    main.clear_flipkart_session(driver)
    assert driver.visited == ["https://www.flipkart.com/"]
    assert driver.cleared == ["cookies for current Flipkart origin"]
    assert "localStorage.clear()" in driver.scripts[0] and "sessionStorage.clear()" in driver.scripts[0]


def test_main_fresh_login_requires_visible_browser(harness):
    main, _, _, log_, _, _, _ = harness
    assert main.main(["--fresh-login"]) == 1
    assert log_ == []


def test_main_no_login_flag_skips_login(harness):
    main, settings, outcome, log_, drivers, logins, report = harness
    assert main.main(["--no-login"]) == 0
    assert "login" not in log_ and json.loads(report.read_text())["login"]["status"] == "disabled"


def test_main_signs_in_again_after_browser_restart(harness, monkeypatch):
    main, settings, outcome, log_, drivers, logins, report = harness
    calls = {"n": 0}

    def flaky(driver, url, name, st, sel, dirs):
        calls["n"] += 1
        if calls["n"] == 1:
            driver.dead = True                                    # first product kills the browser
            return {"product": name, "status": "error", "failure_reason": "boom", "warnings": []}
        log_.append(f"scrape:{name}")
        return {"product": name, "status": "success", "failure_reason": "", "warnings": []}
    monkeypatch.setattr(main, "process_product", flaky)
    assert main.main([]) == 0
    assert log_.count("login") == 2 and len(drivers) == 2          # fresh browser => fresh login
