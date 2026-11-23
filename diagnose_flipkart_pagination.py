"""Read-only, anonymous diagnostic for Flipkart review pagination.

This script does not call the scraper's login flow, read SMS/Gmail, click OTP
controls, or edit production configuration. It uses a dedicated Chrome profile
and saves HTML/screenshots only for pages with no parsed review containers or a
browser/navigation error.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from fk_browser import make_driver
from fk_common import load_config
from fk_parsing import (
    build_review_url,
    classify_page,
    parse_reviews_detailed,
    review_key,
    review_window_state,
    with_page,
)


PRODUCT_URL = (
    "https://www.flipkart.com/boat-airdopes-161-adv-4mic-enx-50h-battery-"
    "fast-charge-13mm-driver-metallic-finish-bluetooth/p/itmc28324fa1e1dd"
    "?pid=ACCHY8GFZUFNZ92H&lid=LSTACCHY8GFZUFNZ92H3HFICX&marketplace=FLIPKART"
    "&q=boat+airdopes&store=0pm%2Ffcn%2F821&spotlightTagId=default_BestsellerId_0pm%2Ffcn%2F821"
    "&srno=s_1_4&otracker=AS_Query_OrganicAutoSuggest_2_4_na_na_ps"
    "&otracker1=AS_Query_OrganicAutoSuggest_2_4_na_na_ps&fm=search-autosuggest"
    "&iid=b42729b2-a52c-4a38-a562-3557cd946d81.ACCHY8GFZUFNZ92H.SEARCH"
    "&ppt=sp&ppn=sp&ssid=1qbnt0dts00000001793813233023"
    "&qH=7c82039a894b5615&ov_redirect=true&ov_redirect=true"
)
START_DATE = "2026-09-01"
END_DATE = "2026-09-22"
SORT_ORDER = "MOST_RECENT"
PAGES_TO_CHECK = tuple(range(13, 23))  # includes 14, 18, 19, 20, and 21

_PAGE_STATE_JS = r"""
const visible = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
const norm = e => (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ');
const nums = [...document.querySelectorAll('a,button,[role="button"]')]
  .filter(e => visible(e) && /^\d+$/.test(norm(e)));
const activeNums = nums.filter(e => e.getAttribute('aria-current') === 'page' ||
  e.getAttribute('aria-selected') === 'true' || /active|selected|current/i.test(String(e.className)));
const sortNames = ['Latest','Most Helpful','Positive','Negative'];
const sorts = sortNames.map(label => {
  const e = [...document.querySelectorAll('button,[role="button"],a')]
    .find(x => visible(x) && norm(x) === label);
  return e ? {label, ariaPressed:e.getAttribute('aria-pressed'), ariaSelected:e.getAttribute('aria-selected'),
    className:String(e.className || '')} : {label, present:false};
});
const nexts = [...document.querySelectorAll('a,button,[role="button"]')]
  .filter(e => visible(e) && /^(next|›|»|❯)$/i.test(norm(e)))
  .slice(0,6).map(e => ({text:norm(e), disabled:!!e.disabled || e.getAttribute('aria-disabled') === 'true' ||
    /disabled/i.test(String(e.className || '')), href:e.href || ''}));
const bodyText = document.body ? document.body.innerText : '';
const loading = [...document.querySelectorAll('[aria-busy="true"],.loading,.loader,[class*="spinner"]')]
  .some(visible);
return {readyState:document.readyState, activePageLabels:activeNums.map(norm), sortControls:sorts,
  nextControls:nexts, loadingIndicatorVisible:loading,
  loadingSentinelVisible:/hang on, loading content/i.test(bodyText)};
"""

_EMPTY_MARKER_RE = re.compile(
    r"\b(no reviews found|no reviews yet|no customer reviews|be the first to review|"
    r"there are no reviews|no ratings and reviews)\b",
    re.I,
)


def _safe_url(url: str) -> str:
    """Keep the requested URL visible while redacting any auth-like query values."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in list(query):
        if re.search(r"token|session|auth|cookie|password|otp", key, re.I):
            query[key] = ["<redacted>"]
    from urllib.parse import urlencode

    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params,
                       urlencode(query, doseq=True), parsed.fragment))


def _review_fingerprint(review) -> str:
    raw = "\x1f".join(str(v or "") for v in review_key(review))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    project = Path(__file__).resolve().parent
    diagnostics = project / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = diagnostics / f"pagination_probe_{stamp}.json"
    profile = project / ".chrome-profile-pagination-diagnostic"
    settings, selectors = load_config()
    wait_seconds = max(5, int(settings.get("page_wait_seconds", 25)))
    start = datetime.fromisoformat(START_DATE).date()
    end = datetime.fromisoformat(END_DATE).date()
    review_url = build_review_url(PRODUCT_URL)
    if not review_url:
        print("SETUP_ERROR: could not construct the Flipkart reviews URL")
        return 2

    print("LOGIN=disabled (this script never calls the login flow)")
    print(f"PROFILE={profile}")
    print(f"DATE_RANGE={START_DATE}..{END_DATE} inclusive")
    print(f"SORT_ORDER={SORT_ORDER}; pages={PAGES_TO_CHECK[0]}..{PAGES_TO_CHECK[-1]}")
    print("Starting Chrome with a dedicated diagnostic profile; no OTP will be requested.", flush=True)

    try:
        driver = make_driver(headless=False, profile_dir=str(profile))
    except Exception as exc:
        print(f"BROWSER_START_ERROR={type(exc).__name__}: {str(exc).splitlines()[0][:240]}")
        print("No Flipkart page was requested. Paste this terminal output back.")
        return 2

    results = []
    fingerprints_seen: set[str] = set()
    try:
        driver.set_page_load_timeout(60)
        try:
            driver.get(review_url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop()")
            except WebDriverException:
                pass
        except WebDriverException as exc:
            print(f"INITIAL_NAVIGATION_ERROR={type(exc).__name__}: {str(exc).splitlines()[0][:240]}")
            print("No page sequence was tested. Paste this terminal output back.")
            return 2

        base_url = driver.current_url or review_url
        try:
            WebDriverWait(driver, 20).until(lambda d: d.execute_script("return document.readyState") == "complete")
        except Exception:
            pass

        selector_counts = list(selectors.get("review_block", []))
        for requested_page in PAGES_TO_CHECK:
            requested_url = with_page(base_url, requested_page, SORT_ORDER)
            row = {
                "requested_page": requested_page,
                "requested_url": _safe_url(requested_url),
                "sort_order_requested": SORT_ORDER,
                "navigation_error": "",
                "timed_out": False,
            }
            try:
                driver.get(requested_url)
            except TimeoutException as exc:
                row["timed_out"] = True
                row["navigation_error"] = f"TimeoutException: {str(exc).splitlines()[0][:180]}"
                try:
                    driver.execute_script("window.stop()")
                except WebDriverException:
                    pass
            except WebDriverException as exc:
                row["navigation_error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}"

            try:
                WebDriverWait(driver, 15).until(lambda d: d.execute_script("return document.readyState") == "complete")
            except Exception:
                pass

            parsed = {"reviews": [], "detected": 0, "invalid": 0, "tier": "none"}
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                try:
                    parsed = parse_reviews_detailed(driver.page_source, selectors)
                except WebDriverException as exc:
                    row["navigation_error"] = row["navigation_error"] or f"{type(exc).__name__}: page source unavailable"
                    break
                if parsed["reviews"]:
                    break
                time.sleep(0.5)

            try:
                actual_url = driver.current_url or ""
                title = driver.title or ""
                body_text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
                state = driver.execute_script(_PAGE_STATE_JS)
                html = driver.page_source
            except WebDriverException as exc:
                actual_url, title, body_text, html = "", "", "", ""
                state = {"readyState": "unavailable", "error": type(exc).__name__}
                row["navigation_error"] = row["navigation_error"] or f"{type(exc).__name__}: browser state unavailable"

            actual_query = parse_qs(urlparse(actual_url).query)
            row.update({
                "actual_url": _safe_url(actual_url),
                "url_page_number": (actual_query.get("page") or [""])[0],
                "url_sort_order": (actual_query.get("sortOrder") or [""])[0],
                "displayed_page_labels": state.get("activePageLabels", []),
                "selected_sort_controls": state.get("sortControls", []),
                "next_controls": state.get("nextControls", []),
                "document_ready_state": state.get("readyState", "unavailable"),
                "loading_indicator_visible": state.get("loadingIndicatorVisible", False),
                "loading_sentinel_visible": state.get("loadingSentinelVisible", False),
                "review_container_counts_by_selector": {},
                "parsed_review_count": len(parsed.get("reviews", [])),
                "parser_detected_count": parsed.get("detected", 0),
                "parser_tier": parsed.get("tier", "none"),
                "review_dates": [],
                "explicit_empty_message": bool(_EMPTY_MARKER_RE.search(body_text)),
                "page_classifier_state": classify_page(actual_url, title, body_text, parsed.get("detected", 0))[0],
                "title": title[:160],
            })
            for selector in selector_counts:
                try:
                    count = int(driver.execute_script("return document.querySelectorAll(arguments[0]).length", selector))
                except WebDriverException:
                    count = -1
                row["review_container_counts_by_selector"][selector] = count

            current_fingerprints = []
            for review in parsed.get("reviews", []):
                row["review_dates"].append({
                    "display": review.get("Date", ""),
                    "precision": review.get("Date Precision", "none"),
                    "earliest": review.get("Date Earliest", ""),
                    "latest": review.get("Date Latest", ""),
                    "range_state": review_window_state(review, start, end),
                })
                current_fingerprints.append(_review_fingerprint(review))
            row["fingerprint"] = hashlib.sha256("|".join(current_fingerprints).encode("ascii")).hexdigest()[:16]
            row["records_repeated_from_prior_pages"] = sum(1 for fp in current_fingerprints if fp in fingerprints_seen)
            fingerprints_seen.update(current_fingerprints)

            # Do not infer that a page is the end just because its review selector found zero rows.
            # Record the visible evidence separately so the returned HTML can settle that question.
            explicit_end = bool(row["explicit_empty_message"] or any(c.get("disabled") for c in row["next_controls"]))
            row["empty_page_assessment"] = (
                "has_review_containers" if row["parsed_review_count"] else
                "explicit_empty_or_disabled_next_evidence" if explicit_end else
                "zero_review_containers_end_not_verified"
            )

            if row["parsed_review_count"] == 0 or row["navigation_error"]:
                safe_page = f"{SORT_ORDER.lower()}_page_{requested_page}_{stamp}"
                html_path = diagnostics / f"{safe_page}.html"
                png_path = diagnostics / f"{safe_page}.png"
                if html:
                    html_path.write_text(html, encoding="utf-8")
                screenshot_saved = False
                try:
                    screenshot_saved = bool(driver.save_screenshot(str(png_path)))
                except WebDriverException:
                    pass
                row["html_path"] = str(html_path) if html else ""
                row["screenshot_path"] = str(png_path) if screenshot_saved else ""

            results.append(row)
            print("PAGE=" + json.dumps(row, ensure_ascii=False), flush=True)

        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "login_attempted": False,
            "otp_accessed": False,
            "product_url": _safe_url(PRODUCT_URL),
            "reviews_base_url": _safe_url(base_url),
            "date_range_inclusive": [START_DATE, END_DATE],
            "sort_order": SORT_ORDER,
            "pages": results,
        }
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"REPORT_JSON={output_path}")
        print("Paste the PAGE= lines and REPORT_JSON path back. Do not send passwords, cookies, or OTPs.")
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
