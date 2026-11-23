"""
Flipkart login (optional, never bypasses security controls).

Layers (so almost everything can be tested offline):
  1. pure helpers      - credentials, masking/redaction, field detection, page-state classification,
                         OTP extraction and Gmail IMAP
  2. run_login()       - the decision flow, written against a small "page" interface
  3. LiveLoginPage     - the Selenium implementation of that interface

Flow: open login page -> type the login ID -> press the request button -> look at what Flipkart
ACTUALLY shows next (OTP / password / CAPTCHA-verification / error / sign-up):
  * OTP       -> email IDs use authorized Gmail IMAP; phone IDs wait for you to type the OTP
  * password  -> use FKART_PASSWORD if set, otherwise wait for you
  * CAPTCHA / security verification -> pause and wait for you; nothing is solved or bypassed
Success is only reported after an account page loads that is not a login screen.

Passwords and OTPs are not logged by this Python application or put in its reports.
"""
import email
import imaplib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import urlsplit

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from fk_common import env

log = logging.getLogger("fk")

# Login statuses reported to the main program / run_report.json
LOGGED_IN = "logged_in"
FAILED = "failed"
NOT_CONFIGURED = "not_configured"
DISABLED = "disabled"

# Page states seen on the login screen
S_ID, S_OTP, S_PASSWORD, S_CAPTCHA, S_ERROR, S_SIGNUP, S_OTHER = (
    "id_entry", "otp", "password", "captcha", "error", "signup", "other")


# --------------------------------------------------------------------------- #
# 1a. credentials, masking, redaction
# --------------------------------------------------------------------------- #
_PLACEHOLDER = re.compile(r"^(your[_\- ]|new[_\- ]|change[_\- ]?me|example|xxx)", re.I)


def _real(value):
    """Env value, or None when empty / still the placeholder copied from .env.example."""
    return None if not value or _PLACEHOLDER.match(value.strip()) else value.strip()


@dataclass
class Credentials:
    login_id: str = None        # Flipkart email or mobile number
    gmail_user: str = None      # inbox used to read the OTP e-mail
    gmail_app_password: str = field(default=None, repr=False)
    password: str = field(default=None, repr=False)
    login_id_from_legacy: bool = False

    def secrets(self):
        return [v for v in (self.gmail_app_password, self.password) if v]

    @property
    def gmail_ready(self):
        return bool(self.gmail_user and self.gmail_app_password)


#  def load_credentials():
#     login_id = _real(env("FKART_LOGIN_ID"))
#     gmail_user = _real(env("FKART_EMAIL"))
#     legacy = False
#     if not login_id and gmail_user:          # old .env files only had FKART_EMAIL
#         login_id, legacy = gmail_user, True
#     app_pass = _real(env("FKART_APP_PASSWORD"))
#     return Credentials(login_id=login_id, gmail_user=gmail_user,
#                        gmail_app_password=app_pass.replace(" ", "") if app_pass else None,
#                        password=_real(env("FKART_PASSWORD")), login_id_from_legacy=legacy)


def load_credentials():
    login_id = _real(env("FKART_LOGIN_ID"))
    gmail_user = _real(env("FKART_EMAIL"))
    legacy = False

    if not login_id and gmail_user:
        login_id, legacy = gmail_user, True

    if login_id and "@" not in login_id and not is_phone(login_id):
        raise ValueError("FKART_LOGIN_ID must be an email address or phone number")

    app_pass = _real(env("FKART_APP_PASSWORD"))

    # Email login: the same Gmail inbox that receives Flipkart's OTP mail is read over IMAP.
    if not gmail_user and login_id and "@" in login_id:
        gmail_user = login_id

    return Credentials(
        login_id=login_id,
        gmail_user=gmail_user,
        gmail_app_password=app_pass.replace(" ", "") if app_pass else None,
        password=_real(env("FKART_PASSWORD")),
        login_id_from_legacy=legacy,
    )


    

def mask_identifier(value):
    """'someone@gmail.com' -> 'so***@gmail.com', '9876543210' -> '98******10'."""
    v = (value or "").strip()
    if not v:
        return ""
    if "@" in v:
        name, _, domain = v.partition("@")
        return f"{name[:2]}***@{domain}"
    return f"{v[:2]}{'*' * max(len(v) - 4, 2)}{v[-2:]}"


def is_phone(value):
    digits = re.sub(r"[\s+\-()]", "", value or "")
    return digits.isdigit() and 10 <= len(digits) <= 15


def redact(text, secrets):
    out = str(text)
    for s in secrets or []:
        if s:
            out = out.replace(s, "***")
    return out


@dataclass
class LoginResult:
    status: str = FAILED
    method: str = "none"            # saved_session | otp_gmail | otp_manual | password | password_manual | none
    reason: str = ""
    state: str = ""                 # last login-page state seen
    final_url: str = ""             # path only, query string removed
    login_id_masked: str = ""
    manual_steps: list = field(default_factory=list)

    @property
    def logged_in(self):
        return self.status == LOGGED_IN

    def as_dict(self):
        d = asdict(self)
        d["logged_in"] = self.logged_in
        return d


# --------------------------------------------------------------------------- #
# 1b. field detection and page-state classification (pure, offline-testable)
# --------------------------------------------------------------------------- #
_ID_HINTS = ("email", "mobile", "phone", "number", "login", "username")
_ID_TYPES = ("", "text", "email", "tel", "number")


def _blob(i):
    return " ".join(str(i.get(k) or "") for k in ("placeholder", "name", "id", "aria-label", "autocomplete")).lower()


def pick_login_field(inputs):
    """Choose the Flipkart login-ID input among visible inputs (never the header search box,
    a password box, or a single-digit OTP box). `inputs` = list of attribute dicts."""
    best, best_score = None, 0
    plain = []
    for i in inputs:
        t = (i.get("type") or "").lower()
        if not i.get("visible", True) or t not in _ID_TYPES or str(i.get("maxlength") or "") == "1":
            continue
        blob = _blob(i)
        if "search" in blob or (i.get("name") or "").lower() == "q":
            continue
        plain.append(i)
        score = (5 if any(h in blob for h in _ID_HINTS) else 0) + (2 if t in ("email", "tel") else 0)
        score += 1 if any(a in (i.get("autocomplete") or "").lower() for a in ("username", "tel", "email")) else 0
        if score > best_score:
            best, best_score = i, score
    if best is None and len(plain) == 1:
        best = plain[0]
    return best


def pick_otp_inputs(inputs, exclude=None):
    """Six single-character boxes, or one OTP box. Returns a list of attribute dicts (maybe empty)."""
    vis = [i for i in inputs if i.get("visible", True) and (i.get("type") or "").lower() not in ("password", "hidden", "checkbox")]
    boxes = [i for i in vis if str(i.get("maxlength") or "") == "1"]
    if len(boxes) >= 4:
        return boxes
    # Flipkart can render six OTP digits as type=number inputs without maxlength or labels.
    number_boxes = [
        i for i in vis
        if (i.get("type") or "").lower() == "number"
        and "search" not in _blob(i)
        and (i.get("name") or "").lower() != "q"
    ]
    if len(number_boxes) >= 6:
        return number_boxes[-6:]
    hinted = [i for i in vis if "one-time-code" in _blob(i) or "otp" in _blob(i)]
    if hinted:
        return hinted[:1]
    rest = [i for i in vis if i is not exclude and "search" not in _blob(i)]
    return rest[-1:] if rest else []


_CAPTCHA = ("captcha", "i'm not a robot", "i am not a robot", "verify you are human", "verify you are a human",
            "are you a robot", "unusual traffic", "unusual activity", "security check", "security verification",
            "press and hold", "checking your browser", "access denied")
_ERROR = ("incorrect otp", "invalid otp", "wrong otp", "otp expired", "otp has expired", "too many attempts",
          "too many otp", "incorrect password", "invalid password", "something went wrong", "try again after")
_SIGNUP = ("looks like you're new", "looks like you are new", "new to flipkart", "create an account")
_OTP_TEXT = ("enter otp", "resend", "otp has been sent", "otp sent", "sent an otp", "sent you an otp",
             "sent to your", "verify otp")
_LOGIN_PROMPTS = ("request otp", "enter email/mobile", "enter email / mobile")


def classify_login_state(text, inputs):
    """Identify the current login step using visible fields and page text."""
    t = (text or "").lower()
    vis = [i for i in inputs if i.get("visible", True)]

    # Security checks take priority.
    if any(p in t for p in _CAPTCHA):
        return S_CAPTCHA

    if any(p in t for p in _ERROR):
        return S_ERROR

    # Detect an OTP form before considering sign-up wording.
    boxes = [i for i in vis if str(i.get("maxlength") or "") == "1"]
    number_boxes = [
        i for i in vis
        if (i.get("type") or "").lower() == "number"
        and "search" not in _blob(i)
        and (i.get("name") or "").lower() != "q"
    ]
    if (
        len(boxes) >= 6
        or len(number_boxes) >= 6
        or any("one-time-code" in _blob(i) or "otp" in _blob(i) for i in vis)
        or any(p in t for p in _OTP_TEXT)
    ):
        return S_OTP

    # Password form.
    if any((i.get("type") or "").lower() == "password" for i in vis):
        return S_PASSWORD

    # Login identifier form.
    if pick_login_field(vis):
        return S_ID

    # Only classify as sign-up if no login, password, or OTP form was detected.
    if any(p in t for p in _SIGNUP):
        return S_SIGNUP

    return S_OTHER


def verify_account_state(url, text, inputs, markers, authenticated_header=False):
    """True only for a real signed-in account page: not a login URL, no login prompt, and at least one
    account marker present. Heuristic - the markers live in fk_config.json so they can be adjusted."""
    path = urlsplit((url or "").lower()).path
    t = (text or "").lower()
    if "login" in path or any(p in t for p in _LOGIN_PROMPTS):
        return False
    if classify_login_state(text, inputs) in (S_ID, S_OTP, S_PASSWORD, S_CAPTCHA):
        return False
    return bool(authenticated_header) or any(m.lower() in t for m in markers)


# --------------------------------------------------------------------------- #
# 1c. OTP e-mail retrieval (Gmail IMAP, read-only)
# --------------------------------------------------------------------------- #
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_OTP_WORDS = ("otp", "one time password", "one-time password", "verification code", "login code")


@dataclass
class OtpResult:
    otp: str = None
    reason: str = ""
    diagnostic: str = ""


def _message_text(msg):
    parts = [str(msg.get("subject", ""))]
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        raw = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        if ctype == "text/html":
            from bs4 import BeautifulSoup
            raw = BeautifulSoup(raw, "html.parser").get_text(" ")
        parts.append(raw)
    return " ".join(parts)


def sender_allowed(from_header, domains):
    addr = parseaddr(from_header or "")[1].lower()
    domain = addr.rpartition("@")[2]
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in domains)


def message_sent_at(msg):
    try:
        sent = parsedate_to_datetime(msg["Date"])
        return sent if sent.tzinfo else sent.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def extract_otp(msg, since, domains=("flipkart.com",), skew=timedelta(seconds=60)):
    """6-digit OTP from one message, or None. Checks sender domain, send time (unparseable or
    stale dates are rejected) and that the text really is an OTP message."""
    if not sender_allowed(msg.get("From"), domains):
        return None
    sent = message_sent_at(msg)
    if sent is None or sent < since - skew:
        return None
    text = _message_text(msg)
    low = text.lower()
    if not any(w in low for w in _OTP_WORDS):
        return None
    m = re.search(r"(?:otp|one[- ]time password|verification code|login code|code)\D{0,40}?\b(\d{6})\b", text, re.I)
    if m:
        return m.group(1)
    nums = set(re.findall(r"\b(\d{6})\b", text))
    return nums.pop() if len(nums) == 1 else None


class Clock:
    def now(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def utcnow(self):
        return datetime.now(timezone.utc)


def wait_until(fn, timeout, interval, clock):
    """Poll fn() until it returns something truthy or `timeout` seconds pass. Returns the last value."""
    end = clock.now() + timeout
    while True:
        value = fn()
        if value or clock.now() >= end:
            return value
        clock.sleep(interval)


def fetch_otp_from_gmail(user, app_password, since, timeout=60, interval=5, domains=("flipkart.com",),
                         imap_factory=imaplib.IMAP4_SSL, clock=None):
    """Poll Gmail (read-only) for an OTP e-mail sent after `since`. Returns OtpResult; the reason
    never contains the app password or the OTP."""
    clock = clock or Clock()
    state = {"reason": "no matching OTP e-mail arrived in time"}

    def attempt():
        mail = None
        try:
            mail = imap_factory("imap.gmail.com")
            mail.login(user, app_password)
            mail.select("INBOX", readonly=True)
            d = since - timedelta(days=1)
            day = f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"      # strftime %b is locale-dependent
            _, data = mail.search(None, f'(FROM "flipkart" SINCE {day})')
            ids = data[0].split()[-15:] if data and data[0] else []
            found = []
            for mid in reversed(ids):
                _, msg_data = mail.fetch(mid, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                otp = extract_otp(msg, since, domains)
                if otp:
                    found.append((message_sent_at(msg), otp))
            if found:
                return max(found, key=lambda x: x[0])[1]
            state["reason"] = "no matching OTP e-mail arrived in time"
        except imaplib.IMAP4.error:
            state["reason"] = ("Gmail rejected the login (check FKART_EMAIL / FKART_APP_PASSWORD and that "
                               "IMAP is enabled)")
            state["fatal"] = True
            return None
        except Exception as exc:     # network etc. - retry until the deadline
            state["reason"] = f"Gmail connection problem ({type(exc).__name__})"
        finally:
            try:
                if mail:
                    mail.logout()
            except Exception:
                pass
        return None

    otp = wait_until(lambda: attempt() or state.get("fatal"), timeout, interval, clock)
    if otp and otp is not True:
        return OtpResult(otp=otp)
    return OtpResult(reason=state["reason"])


# --------------------------------------------------------------------------- #
def wait_state(page, wanted, timeout, clock, interval=1.0):
    last = {"s": S_OTHER}

    def probe():
        last["s"] = page.state()
        return last["s"] in wanted
    wait_until(probe, timeout, interval, clock)
    return last["s"]


def run_login(page, creds, settings, interactive=True, clock=None, fetch_otp=fetch_otp_from_gmail,
              announce=None):
    clock = clock or Clock()
    announce = announce or log.warning
    step = settings.get("login_step_timeout_seconds", 30)
    manual_wait = settings.get("login_manual_wait_seconds", 240)
    gmail_wait = settings.get("login_gmail_wait_seconds", 60)
    # Seconds you get to answer Flipkart's verification call after the OTP is submitted.
    call_wait = max(0.0, float(settings.get("login_call_answer_wait_seconds", 30)))
    domains = tuple(settings.get("otp_sender_domains") or ("flipkart.com",))
    res = LoginResult(login_id_masked=mask_identifier(creds.login_id))
    phone_confirmation_pending = False

    def fail(reason, state=""):
        res.status, res.state = FAILED, state or res.state
        res.reason = redact(reason, creds.secrets())
        res.final_url = page.current_path()
        return res

    def human(task):
        """Pause for the person at the browser. Returns an error string if that is not possible."""
        if not interactive:
            return f"{task} is required but the browser is headless - run without --headless"
        res.manual_steps.append(task)
        announce("ACTION REQUIRED in the Chrome window: %s (waiting up to %d s). "
                 "Complete it yourself; the script continues automatically.", task, manual_wait)
        return None

    def clear_captcha():
        if page.state() != S_CAPTCHA:
            return None
        problem = human("complete the security verification / CAPTCHA")
        if problem:
            return problem
        wait_until(lambda: page.state() != S_CAPTCHA, manual_wait, 2, clock)
        return "security verification was not completed in time" if page.state() == S_CAPTCHA else None

    def after_submit_ok(task):
        """Wait (up to `task` seconds) for the OTP/password step to disappear; return the page state."""
        wait_until(lambda: page.state() not in (S_OTP, S_PASSWORD), task, 2, clock)
        return page.state()

    page.open_login()
    state = wait_state(page, {S_ID, S_CAPTCHA, S_ERROR, S_SIGNUP, S_OTHER}, step, clock)

    # A dedicated Selenium profile keeps Flipkart's login cookie between form runs.
    # Flipkart may redirect /account/login directly to an account page when that
    # profile already has an authenticated session.
    if state == S_OTHER:
        if settings.get("_require_login_challenge"):
            return fail(
                "fresh login was requested but Flipkart did not show the phone/email login form after session clearing",
                state,
            )
        existing_session = getattr(page, "account_verified_current", None)
        if callable(existing_session) and existing_session():
            res.status, res.reason, res.state = LOGGED_IN, "", "account_page_verified"
            res.method = "saved_session"
            res.final_url = page.current_path()
            return res
        # Some account URLs do not show profile markers until the account page is
        # requested explicitly. This check is safe here, before any OTP challenge.
        verify_existing = getattr(page, "account_verified", None)
        if callable(verify_existing) and verify_existing():
            res.status, res.reason, res.state = LOGGED_IN, "", "account_page_verified"
            res.method = "saved_session"
            res.final_url = page.current_path()
            return res
        state = page.state()

    if state == S_CAPTCHA:
        problem = clear_captcha()
        if problem:
            return fail(problem, S_CAPTCHA)
        state = wait_state(page, {S_ID, S_CAPTCHA, S_ERROR, S_SIGNUP, S_OTHER}, step, clock)

    if state == S_CAPTCHA:
        return fail(
            "security verification was not completed in time",
            S_CAPTCHA,
        )

    if state != S_ID:
        return fail(
            f"login field not found (the page shows: {state})",
            state,
        )

    
    if "@" in (creds.login_id or ""):
        # Flipkart opens in phone-number mode (digits only); switch to Email-ID before typing.
        switcher = getattr(page, "switch_to_email_login", None)
        if callable(switcher) and not switcher():
            return fail("could not switch Flipkart's login form to Email-ID", state)
    if not page.type_id(creds.login_id):
        return fail("could not type into the login field")
    started = clock.utcnow()
    if not page.click_submit():
        return fail("the Request OTP / Continue button was not found")

    for _ in range(4):                    # a verification step can appear before or after the OTP
        state = wait_state(page, {S_OTP, S_PASSWORD, S_CAPTCHA, S_ERROR, S_SIGNUP}, step, clock)
        res.state = state
        if state == S_CAPTCHA:
            problem = clear_captcha()
            if problem:
                return fail(problem, state)
            continue
        if state == S_ERROR:
            return fail("Flipkart showed an error message on the login screen", state)
        if state == S_SIGNUP:
            return fail("Flipkart does not recognise this login ID (sign-up page). "
                        "No account is created automatically", state)

        if state == S_OTP:
            phone_confirmation_pending = True      # Flipkart may phone you after the OTP, for any login ID
            if is_phone(creds.login_id):
                # Phone SMS codes are not read automatically; the OTP is typed in by the person.
                otp_result, otp_method = OtpResult(reason="phone OTP is entered manually"), "otp_manual"
            else:
                otp_result = OtpResult(reason="Gmail not configured (FKART_EMAIL / FKART_APP_PASSWORD)")
                if not creds.gmail_ready:
                    return fail("OTP is read from Gmail only; set FKART_APP_PASSWORD (Gmail app password) in .env",
                                state)
                otp_result = fetch_otp(
                    creds.gmail_user, creds.gmail_app_password, started, gmail_wait, 5, domains
                )
                otp_method = "otp_gmail"
            if otp_result.otp:
                if not (page.fill_otp(otp_result.otp) and page.click_verify()):
                    return fail("OTP was found but could not be entered into the page", state)
                res.method = otp_method
                final = after_submit_ok(step)
            else:
                log.warning("Automatic OTP retrieval did not return a code; manual entry may be required.")
                problem = human(f"type the OTP sent to {res.login_id_masked} into the page and press Verify")
                if problem:
                    return fail(f"Automatic OTP retrieval failed; {problem}", state)
                res.method = "otp_manual"
                final = after_submit_ok(manual_wait)
            if final == S_ERROR:
                return fail("Flipkart rejected the OTP", final)
            if final == S_OTP:
                return fail("the page still shows the OTP step (OTP not accepted or not submitted)", final)
            if final == S_CAPTCHA:
                continue
            break

        if state == S_PASSWORD:
            if creds.password:
                if not (page.fill_password(creds.password) and page.click_verify()):
                    return fail("password could not be entered into the page", state)
                res.method = "password"
                final = after_submit_ok(step)
            else:
                problem = human("enter your Flipkart password in the page and press Login")
                if problem:
                    return fail(f"password requested but FKART_PASSWORD is not set; {problem}", state)
                res.method = "password_manual"
                final = after_submit_ok(manual_wait)
            if final == S_ERROR:
                return fail("Flipkart rejected the password", final)
            if final == S_PASSWORD:
                return fail("the page still shows the password step (not accepted or not submitted)", final)
            if final == S_CAPTCHA:
                continue
            break
        break                              # state S_OTHER: page may already have moved on

    if getattr(page, "session_lost", False):
        return fail("the browser session expired during login; reopen the browser and run login again", res.state)

    # First observe the page without navigating. In particular, Flipkart may make a
    # phone call after accepting an SMS OTP; navigating away during that confirmation
    # can send the browser back to the phone-number form and cancel the login flow.
    def current_account_probe():
        if getattr(page, "session_lost", False):
            return True
        probe = getattr(page, "account_verified_current", None)
        return bool(probe()) if callable(probe) else False

    if phone_confirmation_pending:
        confirmation_wait = call_wait
        announce(
            "ACTION REQUIRED: if Flipkart calls your phone for verification, ANSWER IT NOW and follow "
            "the prompt. Flipkart does not call on every login. Waiting up to %d seconds.",
            int(confirmation_wait),
        )
        signed_in = wait_until(current_account_probe, confirmation_wait, 2, clock)
    else:
        signed_in = wait_until(current_account_probe, 20, 2, clock)

    if getattr(page, "session_lost", False):
        return fail("the browser session expired during login; reopen the browser and run login again", res.state)

    # If Flipkart has already redirected to a signed-in page, accept that result.
    if signed_in:
        if getattr(page, "session_lost", False):
            return fail("the browser session expired during login; reopen the browser and run login again", res.state)
        res.status, res.reason, res.state = LOGGED_IN, "", "account_page_verified"
        res.final_url = page.current_path()
        return res

    # Do one explicit account-page check after the observation window, rather than
    # repeatedly navigating while a phone confirmation may still be in progress.
    if page.account_verified():
        res.status, res.reason, res.state = LOGGED_IN, "", "account_page_verified"
        res.final_url = page.current_path()
        return res

    if phone_confirmation_pending:
        return fail(
            "Flipkart sign-in was not confirmed after the phone verification wait; "
            "answer the verification call within the %d s window and run --login-only again" % int(call_wait),
            res.state,
        )
    return fail("could not confirm a signed-in account page after the login steps", res.state)


# --------------------------------------------------------------------------- #
# 3. Selenium implementation
# --------------------------------------------------------------------------- #
_CLICK_XPATH = ("//button | //input[@type='submit' or @type='button'] | //a | //*[@role='button'] "
                "| //span[not(*)] | //div[not(*)]")
_ATTRS = ("type", "placeholder", "name", "id", "aria-label", "autocomplete", "maxlength")
_TAG_RANK = {"button": 0, "input": 0, "a": 1, "span": 2, "div": 2}


class LiveLoginPage:
    def __init__(self, driver, settings):
        self.driver, self.settings = driver, settings
        self.markers = settings.get("login_success_markers") or []
        self._id_el = None
        self._session_lost = False

    @property
    def session_lost(self):
        return self._session_lost

    # -- helpers
    def _inputs(self):
        out = []
        for el in self.driver.find_elements(By.TAG_NAME, "input"):
            try:
                d = {k: el.get_attribute(k) for k in _ATTRS}
                d["visible"], d["el"] = bool(el.is_displayed()), el
                out.append(d)
            except WebDriverException:
                continue
        return out

    def _text(self):
        from fk_browser import page_text
        return page_text(self.driver)

    def _click(self, words):
        from fk_browser import js_click
        cands = []
        for el in self.driver.find_elements(By.XPATH, _CLICK_XPATH):
            try:
                if not el.is_displayed():
                    continue
                label = " ".join(((el.text or "") or (el.get_attribute("value") or "")).split()).lower()
                if not label or len(label) > 40:
                    continue
                for rank, w in enumerate(words):
                    if w in label:
                        cands.append((rank, 0 if label == w else 1, _TAG_RANK.get(el.tag_name.lower(), 2), el))
                        break
            except WebDriverException:
                continue
        if not cands:
            return False
        cands.sort(key=lambda c: c[:3])
        js_click(self.driver, cands[0][3])
        return True
    
    def _email_field_visible(self):
        for i in self._inputs():
            if not i.get("visible", True):
                continue
            if (i.get("type") or "").lower() == "email" or "email" in _blob(i):
                return True
        return False

    def switch_to_email_login(self):
        """Click 'Use Email-ID' so the login box accepts an e-mail address instead of digits only."""
        if self._email_field_visible():
            return True
        clicked = self._click(["use email-id", "use email id", "use email"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._email_field_visible():
                return True
            time.sleep(0.3)
        log.info("LOGIN DIAGNOSTIC: 'Use Email-ID' clicked=%s, email field detected=False", clicked)
        return clicked      # layout changed but the click worked: let type_id use whatever field is shown

    @staticmethod
    def _send(el, text):
        try:
            el.clear()
        except WebDriverException:
            pass
        el.send_keys(text)

    # -- page interface
    def open_login(self):
        """Open the login form even when Flipkart redirects its login URL home.

        Some Flipkart responses send `/account/login` to the homepage, where the
        sign-in form is available only after selecting the visible Login control.
        This clicks that control only when no login/OTP/password form is present.
        """
        from fk_browser import safe_get
        safe_get(self.driver, self.settings.get("login_url", "https://www.flipkart.com/account/login"), 1, 20)
        if classify_login_state(self._text(), self._inputs()) != S_OTHER:
            return
        if not self._click(["login"]):
            return
        timeout = max(1, min(30, int(self.settings.get("login_step_timeout_seconds", 10))))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if classify_login_state(self._text(), self._inputs()) != S_OTHER:
                log.info("LOGIN DIAGNOSTIC: opened Flipkart's visible Login dialog after homepage redirect")
                return
            time.sleep(0.25)

    
    def state(self):
        try:
            text = self._text()
            inputs = self._inputs()
            state = classify_login_state(text, inputs)

            log.info("LOGIN DIAGNOSTIC: detected state=%s", state)
            log.info("LOGIN DIAGNOSTIC: visible input fields=%s", [
                {
                    "type": i.get("type"),
                    "placeholder": i.get("placeholder"),
                    "name": i.get("name"),
                    "id": i.get("id"),
                    "autocomplete": i.get("autocomplete"),
                    "maxlength": i.get("maxlength"),
                }
                for i in inputs if i.get("visible", True)
            ])

            return state
        except WebDriverException as exc:
            if any(term in str(exc).lower() for term in ("invalid session id", "session deleted", "disconnected")):
                self._session_lost = True
            return S_OTHER

    def type_id(self, text):
        field_ = pick_login_field(self._inputs())
        if not field_:
            return False
        self._id_el = field_["el"]
        self._send(self._id_el, text)
        return True

    def click_submit(self):
        if self._click(["request otp", "continue", "login"]):
            return True
        if self._id_el is not None:              # fall back to pressing Enter in the field
            try:
                self._id_el.send_keys(Keys.RETURN)
                return True
            except WebDriverException:
                pass
        return False

    def fill_otp(self, otp):
        inputs = self._inputs()
        target = pick_otp_inputs(inputs, exclude=self._id_el_attrs(inputs))
        if len(target) >= 4 and len(target) >= len(otp):
            for box, ch in zip(target, otp):
                box["el"].send_keys(ch)
            return True
        if target:
            self._send(target[0]["el"], otp)
            return True
        return False

    def _id_el_attrs(self, inputs):
        return next((i for i in inputs if i["el"] is self._id_el), None)

    def fill_password(self, password):
        for i in self._inputs():
            if i["visible"] and (i["type"] or "").lower() == "password":
                self._send(i["el"], password)
                return True
        return False

    def click_verify(self):
        return self._click(["verify", "login", "submit"])

    def click_resend_otp(self):
        """Wait briefly for the OTP resend cooldown, then click one enabled Resend control."""
        timeout = max(0.0, float(self.settings.get("login_resend_wait_seconds", 15)))
        deadline = time.monotonic() + timeout
        while True:
            try:
                candidates = self.driver.find_elements(By.XPATH, "//button | //a | //*[@role='button']")
            except WebDriverException as exc:
                if any(term in str(exc).lower() for term in (
                    "invalid session id", "session deleted", "disconnected",
                )):
                    self._session_lost = True
                return False
            for el in candidates:
                try:
                    if not el.is_displayed():
                        continue
                    label = " ".join(((el.text or "") or (el.get_attribute("value") or "")).split()).lower()
                    if "resend" not in label:
                        continue
                    if not getattr(el, "is_enabled", lambda: True)():
                        continue
                    if (el.get_attribute("disabled") is not None
                            or (el.get_attribute("aria-disabled") or "").lower() == "true"):
                        continue
                    from fk_browser import js_click
                    js_click(self.driver, el)
                    return True
                except WebDriverException:
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.5, remaining))

    def account_verified(self):
        from fk_browser import safe_get
        if self._session_lost:
            return False
        try:
            safe_get(self.driver, self.settings.get("login_verify_url", "https://www.flipkart.com/account/"), 0, 20)
            return verify_account_state(
                self.driver.current_url, self._text(), self._inputs(), self.markers,
                authenticated_header=self._has_signed_in_account_menu(),
            )
        except WebDriverException as exc:
            if any(term in str(exc).lower() for term in ("invalid session id", "session deleted", "disconnected")):
                self._session_lost = True
            return False

    def account_verified_current(self):
        """Check the current page for a signed-in account without changing its URL."""
        if self._session_lost:
            return False
        try:
            return verify_account_state(
                self.driver.current_url, self._text(), self._inputs(), self.markers,
                authenticated_header=self._has_signed_in_account_menu(),
            )
        except WebDriverException as exc:
            if any(term in str(exc).lower() for term in ("invalid session id", "session deleted", "disconnected")):
                self._session_lost = True
            return False

    def _has_signed_in_account_menu(self):
        """Recognize the signed-in name/dropdown control without reading or logging its label."""
        return bool(self.driver.execute_script(r"""
            /* flipkart-account-menu-probe */
            const visible = el => {
              const r = el.getBoundingClientRect();
              const s = getComputedStyle(el);
              return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            const excluded = /^(login|log in|sign in|sign up|signup|more|cart|wishlist|become a seller)$/i;
            const nodes = document.querySelectorAll(
              'header a, header button, header [role="button"], header [aria-haspopup], ' +
              'a[aria-haspopup], button[aria-haspopup], [role="button"][aria-haspopup], ' +
              'a[aria-expanded="true"], button[aria-expanded="true"], [role="button"][aria-expanded="true"]'
            );
            for (const el of nodes) {
              if (!visible(el)) continue;
              const r = el.getBoundingClientRect();
              if (r.left < window.innerWidth * 0.55 || r.top > 180) continue;
              const label = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '')
                .replace(/\s+/g, ' ').trim();
              if (!label || label.length > 80 || excluded.test(label)) continue;
              if (/\b(login|log in|sign in|sign up|signup)\b/i.test(label)) continue;
              const attrs = [el.getAttribute('aria-haspopup'), el.getAttribute('aria-label'), el.title,
                ...Array.from(el.querySelectorAll('[aria-label], [title]')).flatMap(n =>
                  [n.getAttribute('aria-label'), n.getAttribute('title')])].join(' ');
              const arrowWord = /\b(arrow|chevron|caret|dropdown|expand)\b/i.test(attrs);
              const arrowGlyph = /[▼▾⌄]/.test(el.textContent || '');
              const hasMenuSemantics = !!el.getAttribute('aria-haspopup') || el.getAttribute('aria-expanded') === 'true';
              const hasIcon = !!el.querySelector('svg');
              if (hasMenuSemantics || arrowWord || arrowGlyph || hasIcon) return true;
            }
            return false;
        """))

    def current_path(self):
        try:
            p = urlsplit(self.driver.current_url)
            return f"{p.scheme}://{p.netloc}{p.path}"
        except Exception:
            return ""


def login_to_flipkart(driver, settings, creds=None, clock=None):
    """Public entry point used by the scraper. Always returns a LoginResult; never raises."""
    creds = creds or load_credentials()
    if not settings.get("login_enabled", True):
        return LoginResult(status=DISABLED, reason="login_enabled is false in fk_config.json")
    if not creds.login_id:
        return LoginResult(status=NOT_CONFIGURED,
                           reason="FKART_LOGIN_ID is not set in .env (see .env.example)")
    if creds.login_id_from_legacy:
        log.info("FKART_LOGIN_ID not set - using FKART_EMAIL as the Flipkart login ID (old .env style).")
    try:
        return run_login(LiveLoginPage(driver, settings), creds, settings,
                         interactive=not settings.get("headless", False), clock=clock)
    except Exception as exc:
        return LoginResult(status=FAILED, login_id_masked=mask_identifier(creds.login_id),
                           reason=redact(f"unexpected error during login ({type(exc).__name__}: "
                                         f"{str(exc)[:150]})", creds.secrets()))