"""Supplemental anonymous Flipkart review-page and network diagnostic.

No production code or configuration is changed. This script never imports or
calls the login/OTP flow. It captures DOM snapshots for selected review pages
and metadata about XHR/fetch responses (never response bodies or cookies).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import base64
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from fk_common import load_config
from fk_parsing import build_review_url, classify_page, parse_reviews_detailed, review_key, review_window_state, with_page


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
PAGES_TO_CAPTURE = (13, 14, 15, 18, 19, 20, 21, 22)
EMPTY_MARKERS = re.compile(
    r"\b(no reviews found|no reviews yet|no customer reviews|be the first to review|"
    r"there are no reviews|no ratings and reviews)\b", re.I,
)

PAGE_STATE_JS = r"""
const visible = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
const norm = e => (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ');
const nums = [...document.querySelectorAll('a,button,[role="button"]')]
  .filter(e => visible(e) && /^\d+$/.test(norm(e)));
const activeNums = nums.filter(e => e.getAttribute('aria-current') === 'page' ||
  e.getAttribute('aria-selected') === 'true' || /active|selected|current/i.test(String(e.className)));
const labels = ['Latest','Most Helpful','Positive','Negative'];
const sorts = labels.map(label => {
  const e = [...document.querySelectorAll('button,[role="button"],a')]
    .find(x => visible(x) && norm(x) === label);
  return e ? {label, ariaPressed:e.getAttribute('aria-pressed'), ariaSelected:e.getAttribute('aria-selected'),
    className:String(e.className || '')} : {label, present:false};
});
const bodyText = document.body ? document.body.innerText : '';
const loading = [...document.querySelectorAll('[aria-busy="true"],.loading,.loader,[class*="spinner"]')].some(visible);
return {readyState:document.readyState, activePageLabels:activeNums.map(norm), sortControls:sorts,
  loadingIndicatorVisible:loading, loadingSentinelVisible:/hang on, loading content/i.test(bodyText)};
"""


def safe_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in list(query):
        if re.search(r"token|session|auth|cookie|password|otp", key, re.I):
            query[key] = ["<redacted>"]
    from urllib.parse import urlencode
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params,
                       urlencode(query, doseq=True), parsed.fragment))


def endpoint(url: str) -> str:
    """Network report uses endpoint paths and query names, never query values."""
    parsed = urlparse(url)
    keys = sorted(parse_qs(parsed.query, keep_blank_values=True))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" + (f"?keys={','.join(keys)}" if keys else "")


def summarize_response_body(driver, request_id: str, mime_type: str) -> dict:
    """Inspect response shape in memory; never write or print response contents."""
    try:
        result = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
        body = result.get("body", "")
        if result.get("base64Encoded"):
            raw = base64.b64decode(body)
            decoded = raw.decode("utf-8", errors="replace")
        else:
            decoded = body
        summary = {"body_bytes": len(decoded.encode("utf-8")),
                   "body_contains_review_word": bool(re.search(r"review", decoded, re.I)),
                   "body_contains_date_word": bool(re.search(r"date|created|timestamp", decoded, re.I))}
        try:
            payload = json.loads(decoded)
        except (ValueError, TypeError):
            summary["json_valid"] = False
            return summary
        review_arrays, review_like_objects = [], 0

        def visit(node):
            nonlocal review_like_objects
            if isinstance(node, dict):
                lowered = {str(k).lower(): v for k, v in node.items()}
                keys = set(lowered)
                if keys.intersection({"reviewid", "reviewtext", "reviewtitle", "reviewdate"}) or (
                        "rating" in keys and keys.intersection({"text", "content", "date", "createdat"})):
                    review_like_objects += 1
                for key, value in node.items():
                    if "review" in str(key).lower() and isinstance(value, list):
                        review_arrays.append({"key": str(key)[:80], "length": len(value)})
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(payload)
        summary.update(json_valid=True, review_arrays=review_arrays[:20],
                       review_like_object_count=review_like_objects)
        return summary
    except Exception as exc:
        return {"body_capture": type(exc).__name__}


def drain_network(driver):
    events, request_meta = [], {}
    completed_request_ids, failed_request_ids = set(), set()
    try:
        entries = driver.get_log("performance")
    except Exception as exc:
        return [{"log_error": type(exc).__name__}]

    for entry in entries:
        try:
            msg = json.loads(entry["message"])["message"]
            method, params = msg.get("method", ""), msg.get("params", {})
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                rid = params.get("requestId", "")
                request_meta[rid] = {
                    "url": request.get("url", ""),
                    "method": request.get("method", ""),
                    "type": params.get("type", ""),
                    "post_data_bytes": len(request.get("postData", "").encode("utf-8")),
                    "wall_time": params.get("wallTime"),
                }
                url = request.get("url", "")
                if "page/fetch" in url or "product-reviews" in url:
                    events.append({
                        "event": "request_started",
                        "resource_type": params.get("type", ""),
                        "endpoint": endpoint(url),
                        "method": request.get("method", ""),
                        "request_id": rid,
                        "wall_time": params.get("wallTime"),
                    })
            elif method == "Network.loadingFinished":
                completed_request_ids.add(params.get("requestId", ""))
            elif method == "Network.responseReceived":
                completed_request_ids.add(params.get("requestId", ""))
                response = params.get("response", {})
                kind = params.get("type", "")
                if kind in ("XHR", "Fetch", "Document"):
                    event = {
                        "event": "response",
                        "resource_type": kind,
                        "endpoint": endpoint(response.get("url", "")),
                        "status": response.get("status"),
                        "mime_type": response.get("mimeType", ""),
                        "from_disk_cache": response.get("fromDiskCache", False),
                        "from_service_worker": response.get("fromServiceWorker", False),
                        "request_id": params.get("requestId", ""),
                    }
                    if kind in ("XHR", "Fetch"):
                        event["body_summary"] = summarize_response_body(
                            driver, params.get("requestId", ""), response.get("mimeType", ""))
                    events.append(event)
            elif method == "Network.loadingFailed":
                rid = params.get("requestId", "")
                failed_request_ids.add(rid)
                request = request_meta.get(rid, {})
                kind = params.get("type") or request.get("type", "")
                if kind in ("XHR", "Fetch", "Document", "Script"):
                    events.append({
                        "event": "loading_failed",
                        "resource_type": kind,
                        "endpoint": endpoint(request.get("url", "")) if request.get("url") else "",
                        "method": request.get("method", ""),
                        "post_data_bytes": request.get("post_data_bytes", 0),
                        "error": params.get("errorText", ""),
                        "blocked_reason": params.get("blockedReason", ""),
                        "canceled": params.get("canceled", False),
                        "request_id": rid,
                    })
        except Exception:
            continue

    # Keep started-but-unfinished review requests visible: the previous probe
    # only emitted responses and failures, which hid stalled requests.
    for rid, request in request_meta.items():
        url = request.get("url", "")
        if ("page/fetch" in url or "product-reviews" in url) and rid not in completed_request_ids and rid not in failed_request_ids:
            events.append({
                "event": "request_pending_at_capture",
                "resource_type": request.get("type", ""),
                "endpoint": endpoint(url),
                "method": request.get("method", ""),
                "request_id": rid,
                "wall_time": request.get("wall_time"),
            })

    # Deduplicate identical response summaries within this navigation.
    unique, seen = [], set()
    for event in events:
        key = json.dumps(event, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(event)
    return unique


def make_network_driver(profile: Path):
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-IN")
    options.add_argument(f"--user-data-dir={profile}")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Runtime.enable", {})
    driver.execute_cdp_cmd("Log.enable", {})
    return driver


def main() -> int:
    parser = argparse.ArgumentParser(description="Anonymous Flipkart review page network probe")
    parser.add_argument("--tail", action="store_true",
                        help="probe only pages 20-22 and wait up to 25 seconds per page")
    parser.add_argument("--scroll-check", action="store_true",
                        help="exercise native infinite scroll until it loads the requested rounds or stalls")
    parser.add_argument("--scroll-start-page", type=int, default=0,
                        help="optional numbered seed page; 0 uses the normal unnumbered reviews URL")
    parser.add_argument("--scroll-rounds", type=int, default=30,
                        help="maximum scroll rounds for --scroll-check (default: 30)")
    parser.add_argument("--no-progress-limit", type=int, default=5,
                        help="stop after this many consecutive rounds with no new unique reviews (default: 5)")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    diagnostics = project / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_prefix = ("scroll_network_probe" if args.scroll_check else
                     "tail_network_probe" if args.tail else "pagination_network_probe")
    report_path = diagnostics / f"{report_prefix}_{stamp}.json"
    profile = project / ".chrome-profile-pagination-network-diagnostic"
    settings, selectors = load_config()
    # Keep this follow-up probe short. Network status plus readyState/loading
    # indicators are the target; the earlier probe already waited 25s per page.
    pages_to_capture = (20, 21, 22) if args.tail else PAGES_TO_CAPTURE
    wait_seconds = 25 if args.tail else 6
    start = datetime.fromisoformat(START_DATE).date()
    end = datetime.fromisoformat(END_DATE).date()
    base = build_review_url(PRODUCT_URL)
    if not base:
        print("SETUP_ERROR: could not build a reviews URL")
        return 2

    print("LOGIN=disabled; OTP=not accessed")
    print(f"DATE_RANGE={START_DATE}..{END_DATE}; SORT={SORT_ORDER}; PAGES={pages_to_capture}")
    print(f"PAGE_WAIT_SECONDS={wait_seconds}")
    print(f"PROFILE={profile}")
    print("Starting Chrome with network metadata logging; response bodies/cookies are not saved.", flush=True)
    try:
        driver = make_network_driver(profile)
    except Exception as exc:
        print(f"BROWSER_START_ERROR={type(exc).__name__}: {str(exc).splitlines()[0][:220]}")
        return 2

    results = []
    seen_fingerprints = set()
    try:
        # First navigation matches the scraper's direct MOST_RECENT review URL.
        try:
            driver.get(base)
        except TimeoutException:
            try:
                driver.execute_script("window.stop()")
            except WebDriverException:
                pass
        try:
            WebDriverWait(driver, 20).until(lambda d: d.execute_script("return document.readyState") == "complete")
        except Exception:
            pass
        actual_base = driver.current_url or base
        drain_network(driver)  # omit initial page resources; report per-numbered page below

        if args.scroll_check:
            seed_url = (with_page(actual_base, args.scroll_start_page, SORT_ORDER)
                        if args.scroll_start_page > 0 else actual_base)
            try:
                driver.get(seed_url)
            except TimeoutException:
                try:
                    driver.execute_script("window.stop()")
                except WebDriverException:
                    pass
            try:
                WebDriverWait(driver, 20).until(
                    lambda d: d.execute_script("return document.readyState") == "complete")
            except Exception:
                pass
            # Give the initial batch a short, bounded chance to render.
            time.sleep(5)
            rows = []
            scroll_js = r"""const s=[...document.querySelectorAll('div')].find(e=>e.children.length===0&&/hang on, loading content/i.test(e.textContent||'')); if(s)s.scrollIntoView({block:'end'}); window.scrollTo(0,document.body.scrollHeight); return !!s;"""
            def scroll_fingerprint(review):
                description = re.sub(r"(?:\.\.\.|…)+$", "", str(review.get("Review Description") or "").strip()).strip()
                fields = tuple(str(review.get(key) or "").strip() for key in (
                    "User Name", "Ratings", "Short Review", "Date", "Variant", "Location",
                    "Helpful Votes", "Verified Purchase",
                )) + (description[:80],)
                return hashlib.sha256(repr(fields).encode("utf-8")).hexdigest()
            seen_fingerprints = set()
            no_progress = 0
            for round_no in range(1, max(1, args.scroll_rounds) + 1):
                try:
                    before = parse_reviews_detailed(driver.page_source, selectors)
                    before_reviews = before.get("reviews", [])
                    before_count = len(before_reviews)
                    before_fingerprints = {scroll_fingerprint(r) for r in before_reviews}
                    seen_fingerprints.update(before_fingerprints)
                    sentinel_before = bool(driver.execute_script(scroll_js))
                    deadline = time.monotonic() + 12
                    parsed = before
                    while time.monotonic() < deadline:
                        time.sleep(0.5)
                        parsed = parse_reviews_detailed(driver.page_source, selectors)
                        if len(parsed.get("reviews", [])) > before_count:
                            break
                    state = driver.execute_script(PAGE_STATE_JS)
                    events = drain_network(driver)
                    browser_errors = [
                        {"level": item.get("level", ""), "message": str(item.get("message", ""))[:400]}
                        for item in driver.get_log("browser")
                        if item.get("level", "").lower() in ("severe", "error")
                    ]
                    relevant = [event for event in events if "page/fetch" in event.get("endpoint", "")]
                    after_reviews = parsed.get("reviews", [])
                    after_fingerprints = {scroll_fingerprint(r) for r in after_reviews}
                    new_fingerprints = after_fingerprints - before_fingerprints
                    seen_fingerprints.update(after_fingerprints)
                    no_progress = 0 if new_fingerprints else no_progress + 1
                    row = {
                        "round": round_no,
                        "url_page": (parse_qs(urlparse(driver.current_url).query).get("page") or [""])[0],
                        "ready_state": state.get("readyState", ""),
                        "sentinel_visible_before_scroll": sentinel_before,
                        "review_count_before": before_count,
                        "review_count_after": len(after_reviews),
                        "new_rows_after_scroll": max(0, len(after_reviews) - before_count),
                        "new_unique_rows_after_scroll": len(new_fingerprints),
                        "unique_reviews_seen_total": len(seen_fingerprints),
                        "duplicate_rows_in_snapshot": max(0, len(after_reviews) - len(after_fingerprints)),
                        "review_dates_after": [r.get("Date", "") for r in after_reviews],
                        "page_fetch_events": relevant,
                        "browser_errors": browser_errors,
                        "consecutive_no_progress_rounds": no_progress,
                    }
                    if no_progress >= max(1, args.no_progress_limit):
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        html_path = diagnostics / f"scroll_stall_{stamp}.html"
                        png_path = diagnostics / f"scroll_stall_{stamp}.png"
                        html_path.write_text(driver.page_source, encoding="utf-8")
                        driver.save_screenshot(str(png_path))
                        row["stall_html"] = str(html_path)
                        row["stall_screenshot"] = str(png_path)
                        row["stopped_after_no_progress"] = True
                except Exception as exc:
                    row = {"round": round_no, "diagnostic_error": type(exc).__name__}
                rows.append(row)
                print("SCROLL_SUMMARY=" + json.dumps(row, ensure_ascii=False), flush=True)
                if no_progress >= max(1, args.no_progress_limit):
                    break

            report = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "login_attempted": False,
                "otp_accessed": False,
                "test": (f"native infinite scroll from requested page {args.scroll_start_page}"
                         if args.scroll_start_page > 0 else "native infinite scroll from unnumbered reviews URL"),
                "date_range_inclusive": [START_DATE, END_DATE],
                "sort_order": SORT_ORDER,
                "scroll_rounds": rows,
            }
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"REPORT_JSON={report_path}")
            print("Paste the SCROLL_SUMMARY lines and REPORT_JSON path. Do not paste cookies or OTPs.")
            return 0

        for page_no in pages_to_capture:
            requested_url = with_page(actual_base, page_no, SORT_ORDER)
            row = {"requested_page": page_no, "requested_url": safe_url(requested_url),
                   "sort_order_requested": SORT_ORDER, "timed_out": False, "navigation_error": ""}
            # Drain before navigation so responses belong to this requested page.
            drain_network(driver)
            try:
                driver.get(requested_url)
            except TimeoutException as exc:
                row["timed_out"] = True
                row["navigation_error"] = f"TimeoutException: {str(exc).splitlines()[0][:160]}"
                try:
                    driver.execute_script("window.stop()")
                except WebDriverException:
                    pass
            except WebDriverException as exc:
                row["navigation_error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
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
                body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
                state = driver.execute_script(PAGE_STATE_JS)
                html = driver.page_source
            except WebDriverException as exc:
                actual_url, title, body, html = "", "", "", ""
                state = {"readyState": "unavailable"}
                row["navigation_error"] = row["navigation_error"] or f"{type(exc).__name__}: browser state unavailable"

            query = parse_qs(urlparse(actual_url).query)
            row.update({
                "actual_url": safe_url(actual_url),
                "url_page_number": (query.get("page") or [""])[0],
                "url_sort_order": (query.get("sortOrder") or [""])[0],
                "displayed_page_labels": state.get("activePageLabels", []),
                "sort_controls": state.get("sortControls", []),
                "document_ready_state": state.get("readyState", "unavailable"),
                "loading_indicator_visible": state.get("loadingIndicatorVisible", False),
                "loading_sentinel_visible": state.get("loadingSentinelVisible", False),
                "explicit_empty_message": bool(EMPTY_MARKERS.search(body)),
                "page_classifier_state": classify_page(actual_url, title, body, parsed.get("detected", 0))[0],
                "review_container_counts_by_selector": {},
                "parsed_review_count": len(parsed.get("reviews", [])),
                "parser_detected_count": parsed.get("detected", 0),
                "parser_tier": parsed.get("tier", "none"),
                "review_dates": [],
            })
            for selector in selectors.get("review_block", []):
                try:
                    row["review_container_counts_by_selector"][selector] = int(
                        driver.execute_script("return document.querySelectorAll(arguments[0]).length", selector))
                except WebDriverException:
                    row["review_container_counts_by_selector"][selector] = -1

            page_fingerprints = []
            for review in parsed.get("reviews", []):
                row["review_dates"].append({
                    "display": review.get("Date", ""),
                    "precision": review.get("Date Precision", "none"),
                    "earliest": review.get("Date Earliest", ""),
                    "latest": review.get("Date Latest", ""),
                    "range_state": review_window_state(review, start, end),
                })
                raw_key = "\x1f".join(str(v or "") for v in review_key(review))
                page_fingerprints.append(hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16])
            row["record_fingerprint"] = hashlib.sha256("|".join(page_fingerprints).encode("ascii")).hexdigest()[:16]
            row["records_repeated_from_prior_pages"] = sum(fp in seen_fingerprints for fp in page_fingerprints)
            seen_fingerprints.update(page_fingerprints)

            # Save all requested comparisons, including populated pages 13 and 15.
            base_name = f"network_{SORT_ORDER.lower()}_page_{page_no}_{stamp}"
            html_path = diagnostics / f"{base_name}.html"
            png_path = diagnostics / f"{base_name}.png"
            if html:
                html_path.write_text(html, encoding="utf-8")
            try:
                screenshot_saved = bool(driver.save_screenshot(str(png_path)))
            except WebDriverException:
                screenshot_saved = False
            row["html_path"] = str(html_path) if html else ""
            row["screenshot_path"] = str(png_path) if screenshot_saved else ""
            row["network_events"] = drain_network(driver)
            try:
                row["browser_console"] = [
                    {"level": item.get("level", ""), "message": str(item.get("message", ""))[:500]}
                    for item in driver.get_log("browser")
                    if item.get("level", "").lower() in ("severe", "error")
                ]
            except Exception as exc:
                row["browser_console"] = [{"read_error": type(exc).__name__}]
            results.append(row)
            review_network = [
                event for event in row["network_events"]
                if event.get("event") in ("request_started", "response", "loading_failed", "request_pending_at_capture")
                and ("page/fetch" in event.get("endpoint", "") or "product-reviews" in event.get("endpoint", ""))
            ]
            summary = {
                "page": page_no,
                "url_page": row["url_page_number"],
                "ready": row["document_ready_state"],
                "state": row["page_classifier_state"],
                "reviews_found": row["parsed_review_count"],
                "review_requests": review_network,
                "browser_console_errors": row.get("browser_console", []),
            }
            print("PAGE_SUMMARY=" + json.dumps(summary, ensure_ascii=False), flush=True)

        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "login_attempted": False,
            "otp_accessed": False,
            "product_url": safe_url(PRODUCT_URL),
            "reviews_base_url": safe_url(actual_base),
            "date_range_inclusive": [START_DATE, END_DATE],
            "sort_order": SORT_ORDER,
            "response_bodies_saved": False,
            "pages": results,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"REPORT_JSON={report_path}")
        print("Paste the PAGE_SUMMARY lines and REPORT_JSON path. Do not paste cookies or OTPs.")
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
