# v6

"""Shared helpers: config loading, environment variables, logging."""
import json
import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent



def _load_env_file(path):
    """Minimal .env reader used only when python-dotenv is not installed (so credentials are never
    silently ignored). Existing environment variables win, like python-dotenv. Never logs values."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")        # utf-8-sig: Notepad adds a BOM
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().lstrip("\ufeff"), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


try:  # optional: load secrets from a local .env file
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")
except ImportError:
    _load_env_file(BASE_DIR / ".env")

DEFAULT_SETTINGS = {
    "urls_csv": "Flipkart_Hearables_URLs.csv",
    "output_dir": "reports",
    "diagnostics_dir": "diagnostics",
    "max_pages": 0,
    "chrome_profile_dir": ".chrome-profile",
    "review_sort_orders": ["MOST_RECENT", "MOST_HELPFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST"],
    "headless": False,
    "page_wait_seconds": 25,
    "retries": 2,
    "login_enabled": True,
    "keyword_feedback_enabled": True,
    "max_keyword_chips": 15,
    "negative_ratings": [1, 2, 3],
    "diagnostics_keep_days": 7,
    "max_reviews": 0,
    "page_size": 10,
    "max_scroll_rounds": 0,             # 0 = no fixed round limit; a list ends when Flipkart stops loading reviews
    "scroll_patience": 3,
    "scroll_wait_seconds": 10,
    "end_of_list_quiet_seconds": 4,
    # --- full coverage (v6): every topic x every sort is read, nothing is skipped for time ---
    "multi_section_enabled": True,
    "multi_section_max": 0,             # 0 = no limit on the number of lists
    "visit_all_sections": True,
    "section_sorts": ["MOST_RECENT", "MOST_HELPFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST"],
    "fast_mode": False,                 # True would read only Latest lists + Overall's other sorts
    "run_time_budget_seconds": 0,       # 0 = no whole-run time limit
    "max_section_seconds": 0,           # 0 = no per-list time limit
    "section_retries": 2,               # re-collect a list that failed to load or was interrupted
    "list_recoveries": 2,               # re-trigger loading when the list stalled but the loader is still visible
    "parallel_workers": 4,              # browsers reading lists at the same time (1 = one at a time)
    "block_images": True,               # do not download images: faster page loads
    # --- review date window (inclusive). Empty = no date filtering. fk_config.json sets the real values;
    #     START_DATE / END_DATE environment variables and --start-date / --end-date override them. ---
    "start_date": "",
    "end_date": "",
    "date_stop_streak": 10,             # stop once this many reviews IN A ROW are older than start_date
    # An approximate date (a week / month / year label) is accepted only when EVERY day it could mean lies
    # inside the requested range; one that overlaps a range boundary is rejected when this is False.
    "include_uncertain_dates": False,
    # --- login (see fk_login.py) ---
    "login_required": True,             # stop the run instead of scraping anonymously
    "login_url": "https://www.flipkart.com/account/login",
    "login_verify_url": "https://www.flipkart.com/account/?rd=0&link=home_account",
    "login_success_markers": ["profile information", "manage addresses", "my orders", "logout"],
    "login_step_timeout_seconds": 30,
    "login_gmail_wait_seconds": 60,
    "login_resend_wait_seconds": 45,
    "login_manual_wait_seconds": 240,
    "otp_sender_domains": ["flipkart.com"],
}

DEFAULT_SELECTORS = {
    "review_block": ["div.EKFha-"],
    "rating": [".XQDdHH"],
    "short_review": [".z9E0IG"],
    "description": [".ZmyHeo"],
    "username": ["._2NsDsF.AwS1CA"],
    "date": [".row.gHqwa8"],
    "read_more": ["span.b4x-fr span"],
    "keyword_chip": ["span.AgRA\\+X"],
    "keyword_positive": ["span.WtBCuZ"],
    "keyword_negative": ["span._9VjbDx"],
}


def load_config():
    """Return (settings, selectors). A missing/broken fk_config.json never crashes the run."""
    settings, selectors = dict(DEFAULT_SETTINGS), dict(DEFAULT_SELECTORS)
    path = BASE_DIR / "fk_config.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        settings.update(data.get("settings", {}))
        selectors.update(data.get("selectors", {}))
    except FileNotFoundError:
        logging.getLogger("fk").warning("fk_config.json not found - using built-in defaults")
    except Exception as exc:  # malformed JSON etc.
        logging.getLogger("fk").warning("fk_config.json unreadable (%s) - using defaults", exc)
    # GitHub-hosted checkout cleanup must not erase the runner's signed-in profile.
    profile_override = os.getenv("FKART_CHROME_PROFILE_DIR", "").strip()
    if profile_override:
        settings["chrome_profile_dir"] = os.path.expandvars(profile_override)
    return settings, selectors


def resolve_date_range(settings, cli_start=None, cli_end=None):
    """(start, end) as datetime.date or None. Precedence: CLI flag > START_DATE/END_DATE env > fk_config.json.
    Both ends are INCLUSIVE. Raises ValueError for a malformed date or start > end."""
    from fk_parsing import parse_iso_date
    out = []
    for cli, var, key in ((cli_start, "START_DATE", "start_date"), (cli_end, "END_DATE", "end_date")):
        raw = cli or env(var) or settings.get(key)
        out.append(parse_iso_date(raw) if raw not in (None, "") else None)
    if out[0] and out[1] and out[0] > out[1]:
        raise ValueError(f"start date {out[0]} is after end date {out[1]}")
    return tuple(out)


def env(name, default=None):
    value = os.environ.get(name)
    value = value.strip() if isinstance(value, str) else value
    return value if value not in (None, "") else default


def resolve_dir(name):
    p = Path(name)
    if not p.is_absolute():
        p = BASE_DIR / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def setup_logging():
    log = logging.getLogger("fk")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    try:
        fh = logging.FileHandler(BASE_DIR / "run.log", encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    return log

