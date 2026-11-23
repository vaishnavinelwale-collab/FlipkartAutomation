# v6
#Project Name : boAt - Flipkart Feedback Analyzer
#Author       : boAt R&D (Automation QA)
#Version      : v2.1  (verified against live Flipkart layout, explicit statuses)
#Description  : Collects Flipkart reviews for boAt Hearables and builds Excel reports.
#               Only reviews dated within START_DATE..END_DATE (inclusive; fk_config.json, START_DATE/END_DATE
#               environment variables or --start-date/--end-date) are written to the review sheets.
#
# Exit codes:  0 = every product 'success'
#                            2 = finished, but at least one product was partial/failed (see run_report.json)
#                            1 = fatal error before any product could be processed
#********************************************************************#
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from fk_common import BASE_DIR, load_config, resolve_date_range, resolve_dir, setup_logging
from fk_excel import safe_filename, update_workbook
from fk_parsing import build_review_url, classify_review

log = setup_logging()


# --------------------------------------------------------------------------- #
def load_products(csv_path):
    """Tolerant CSV reader: BOM, any header case, missing names."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = [str(c).strip().lower() for c in df.columns]
    url_col = next((c for c in df.columns if "url" in c or "link" in c), df.columns[0])
    name_col = next((c for c in df.columns if "name" in c), None)
    products = []
    for _, row in df.iterrows():
        url = str(row[url_col]).strip()
        if not url.lower().startswith("http"):
            continue
        name = str(row[name_col]).strip() if name_col and pd.notna(row[name_col]) else ""
        if not name or name.lower() == "nan":
            m = re.search(r"flipkart\.com/([^/]+)/", url)
            name = m.group(1).replace("-", " ")[:25] if m else "product"
        products.append((url, name))
    return products


def classify_sentiment(rating, negative_ratings):
    return "Negative" if rating in negative_ratings else "Positive"


def review_sentiment(r, negative_ratings):
    """'Positive' / 'Negative' for a review, or None if it cannot be placed.

    A star rating decides when there is one. Topic-list excerpts that show no stars carry
    'Inferred Sentiment' (set by fk_collect from the wording) instead."""
    try:
        return classify_sentiment(int(r["Ratings"]), negative_ratings)
    except (TypeError, ValueError, KeyError):
        pass
    hint = r.get("Inferred Sentiment")
    return hint if hint in ("Positive", "Negative") else None


def _build_rows(reviews, negative_ratings, wanted):
    rows = []
    for r in reviews:
        if review_sentiment(r, negative_ratings) == wanted:
            reason, final = classify_review(f"{r.get('Short Review', '')} {r.get('Review Description', '')}")
            rows.append({**r, "Reason": reason, "Consolidated Reason": final})
    return rows


def build_negative_df(reviews, negative_ratings):
    """Negative reviews (rating in negative_ratings, or inferred-negative topic excerpts) with keyword reasons."""
    return _build_rows(reviews, negative_ratings, "Negative")


def build_positive_df(reviews, negative_ratings):
    """Positive reviews (rating NOT in negative_ratings, or inferred-positive topic excerpts) with keyword reasons."""
    return _build_rows(reviews, negative_ratings, "Positive")


def process_product(driver, url, name, settings, selectors, dirs):
    """Scrape one product and write its workbook. Returns the diagnostics/result dict. Never raises."""
    from fk_browser import LivePage, collect_keyword_feedback
    from fk_collect import OK_STATUSES, PARTIAL, SUCCESS, get_window, scrape_product
    started = datetime.now()
    run_date, scraped_at = started.strftime("%Y-%m-%d"), started.strftime("%Y-%m-%d %H:%M:%S")
    window = get_window(settings)                  # review-date window, inclusive; (None, None) = no filter
    page = LivePage(driver, settings, dirs["diag"])
    if hasattr(page, "selectors"):
        page.selectors = selectors
    res, data = scrape_product(page, name, url, settings, selectors, today=started.date())
    if res["status"] not in OK_STATUSES:
        log.error("[%s] %s: %s", name, res["status"].upper(), res["failure_reason"])
        return res
    try:
        reviews = data["reviews"]
        for r in reviews:                          # scraping timestamp is kept apart from the review date
            r["Scraped At"] = scraped_at
        neg_set = set(settings["negative_ratings"])
        neg = build_negative_df(reviews, neg_set)
        pos = build_positive_df(reviews, neg_set)
        res["negative_reviews"] = len(neg)
        res["positive_reviews"] = len(pos)

        keywords = []
        out_of_time = (settings.get("deadline_epoch") and time.time() > settings["deadline_epoch"] + 300) \
            or __import__("fk_collect").STOP.is_set()
        if out_of_time:
            res["notes"].append("keyword feedback skipped: run time limit reached")
        elif settings["keyword_feedback_enabled"]:
            keywords = collect_keyword_feedback(driver, res["final_url"], selectors, settings,
                                                settings["page_wait_seconds"])
            if not keywords:
                res["notes"].append("keyword feedback section not present on page (optional)")

        summary = data["summary"] or {}
        stats = {}
        problems, added = update_workbook(
            str(dirs["out"] / safe_filename(name)), name, run_date,
            summary.get("counts") or {}, summary.get("average"), keywords, neg, pos, stats,
            date_range=window, run_info={"scraped_at": scraped_at, "coverage": res["coverage"],
                                         "stop_reason": res["stop_reason"],
                                         "request_id": settings.get("request_id", ""),
                                         "product_name": name,
                                         "duration": settings.get("request_duration", ""),
                                         "duration_unit": settings.get("request_duration_unit", ""),
                                         "scraping_started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
                                         "scraping_completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                         "report_generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                         "total_extracted_reviews": len(reviews),
                                         "collection_status": res["status"],
                                         "error_details": res.get("failure_reason", "")},
            include_uncertain=settings.get("include_uncertain_dates", True))
        res["negative_rows_written"] = added
        res["positive_rows_written"] = stats.get("pos_added", 0)
        removed = stats.get("neg_placeholders_removed", 0) + stats.get("pos_placeholders_removed", 0)
        if removed:
            res["legacy_placeholder_rows_removed"] = removed
            res["notes"].append(f"removed {removed} placeholder 'Rating only (no review)' row(s) left by an older version")
        gone = stats.get("out_of_range_removed", 0)
        if gone:
            res["notes"].append(f"removed {gone} existing row(s) dated outside the review date range "
                                f"(originals are in the backups folder)")
        if added < len(neg):
            res["notes"].append(f"{len(neg) - added} negative review(s) already present in the workbook")
        if not summary.get("counts"):
            res["notes"].append("rating histogram not verified: Star Ratings sheet shows N/A")
        else:
            res["notes"].append(
                f"{len(pos)} positive and {len(neg)} negative reviews individually extracted; aggregate counts "
                f"({summary.get('total_ratings')} ratings) are on the Star Ratings sheet only")
        if problems:
            res["status"] = PARTIAL
            res["warnings"] += problems
            res["failure_reason"] = "; ".join(filter(None, [res["failure_reason"], *problems]))
    except Exception as exc:
        log.exception("[%s] failed while writing output", name)
        res["status"], res["failure_reason"] = "error", f"could not write workbook: {str(exc)[:200]}"
    log.info("[%s] %s | reviews=%d (neg %d) | histogram=%s | mode=%s", name, res["status"].upper(),
             res["valid_reviews"], res["negative_reviews"], res["histogram_verified"], res["pagination_mode"])
    return res


def cleanup_diagnostics(diag_dir, keep_days):
    cutoff = time.time() - keep_days * 86400
    for f in Path(diag_dir).glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def send_alert(results):
    bad = [r for r in results if r["status"] != "success"]
    if not bad:
        return
    try:
        from Auto_Email_Flipkart_OnlyJk import send_alert as _alert
        lines = [f"- {r['product']}: {r['status'].upper()} | {r['failure_reason'] or '; '.join(r['warnings'])}" for r in bad]
        _alert("⚠ BFAT Flipkart scraper needs attention",
               "The Flipkart scraper finished with problems:\n\n" + "\n".join(lines) +
               "\n\nHTML/screenshot snapshots are in the 'diagnostics' folder. "
               "Update the selectors in fk_config.json if Flipkart changed its layout.")
    except Exception as exc:
        log.info("Alert e-mail not sent (optional): %s", exc)


REPORT_PATH = BASE_DIR / "run_report.json"


def fresh_login_profile_dir(configured_profile_dir):
    """Return an isolated sibling profile used only by explicit fresh-login runs."""
    profile = Path(configured_profile_dir or ".chrome-profile")
    return str(profile.with_name(profile.name + "-fresh-login"))


def clear_flipkart_session(driver):
    """Clear Flipkart cookies and web storage in the dedicated fresh-login profile only."""
    driver.get("https://www.flipkart.com/")
    driver.delete_all_cookies()
    driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
    log.info("Cleared saved Flipkart browser authentication for a fresh login.")


def _open_driver(factory, headless, profile_dir, block_images):
    """Call the browser factory, passing block_images only if the factory accepts it.
    (Older/stub factories take just (headless, profile_dir); the real make_driver takes all three.)"""
    import inspect
    try:
        params = inspect.signature(factory).parameters.values()
        takes_three = any(p.kind is p.VAR_POSITIONAL for p in params) or \
            sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params) >= 3
    except (TypeError, ValueError):
        takes_three = True
    if takes_three:
        return factory(headless, profile_dir, block_images)
    return factory(headless, profile_dir)


def start_session(settings, skip_login=False, fresh_login=False):
    """Start Chrome and (optionally) sign in to Flipkart. Returns (driver, LoginResult).
    The same driver - and therefore the same signed-in session - is reused for every product."""
    from fk_browser import make_driver
    from fk_login import DISABLED, FAILED, LoginResult, login_to_flipkart
    profile_dir = settings.get("chrome_profile_dir")
    if fresh_login:
        profile_dir = fresh_login_profile_dir(profile_dir)
    driver = _open_driver(make_driver, settings["headless"], profile_dir, bool(settings.get("block_images", True)))
    if skip_login:
        result = LoginResult(status=DISABLED, reason="--no-login was given")
    else:
        try:
            if fresh_login:
                clear_flipkart_session(driver)
                settings["_require_login_challenge"] = True
            result = login_to_flipkart(driver, settings)
        except Exception as exc:                      # login must never crash the run
            result = LoginResult(status=FAILED, reason=f"unexpected error ({type(exc).__name__})")
    if result.logged_in:
        log.info("LOGIN OK: signed in (%s) as %s", result.method, result.login_id_masked)
    elif result.status == FAILED:
        log.warning("LOGIN FAILED: %s", result.reason)
    else:
        log.info("LOGIN SKIPPED (%s): %s", result.status, result.reason)
    return driver, result


def write_report(results, login, settings=None):
    REPORT_PATH.write_text(
        json.dumps({"run_at": datetime.now().isoformat(timespec="seconds"),
                    "review_date_range": {"start": (settings or {}).get("start_date", ""),
                                          "end": (settings or {}).get("end_date", "")},
                    "login": login.as_dict() if login else None, "results": results}, indent=2),
        encoding="utf-8")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="boAt Flipkart feedback analyzer")
    ap.add_argument("--only", help="process only products whose name contains this text (e.g. --only \"Airdopes 800\")")
    ap.add_argument("--limit", type=int, help="process only the first N products")
    ap.add_argument("--product-url", help="scrape one supplied Flipkart product URL instead of the bulk URL CSV")
    ap.add_argument("--headless", action="store_true", help="run Chrome without a window (no manual OTP/CAPTCHA possible)")
    ap.add_argument("--show-browser", action="store_true",
                    help="show Chrome for first-time sign-in or browser verification")
    ap.add_argument("--login-only", action="store_true",
                    help="only test the Flipkart login, then exit (0 = signed in, 1 = not signed in)")
    ap.add_argument("--fresh-login", action="store_true",
                    help="use an isolated profile, clear Flipkart session data, and sign in fresh (requires --show-browser)")
    ap.add_argument("--no-login", action="store_true", help="skip the login step for this run")
    ap.add_argument("--start-date", help="first review date to keep, YYYY-MM-DD, inclusive "
                                         "(overrides START_DATE env / fk_config.json)")
    ap.add_argument("--end-date", help="last review date to keep, YYYY-MM-DD, inclusive "
                                       "(overrides END_DATE env / fk_config.json)")
    args = ap.parse_args(argv)

    if args.fresh_login and (not args.show_browser or args.no_login):
        log.error("--fresh-login requires --show-browser and cannot be combined with --no-login")
        return 1
    if args.product_url and (args.only or args.limit):
        log.error("--product-url cannot be combined with --only or --limit")
        return 1

    settings, selectors = load_config()
    try:
        start, end = resolve_date_range(settings, args.start_date, args.end_date)
    except ValueError as exc:
        log.error("Bad review date range: %s", exc)
        return 1
    settings["start_date"], settings["end_date"] = (start.isoformat() if start else ""), (end.isoformat() if end else "")
    login_required = bool(settings.get("login_required") or args.fresh_login)
    log.info("Review date range: %s to %s (inclusive)", settings["start_date"] or "any", settings["end_date"] or "any")
    if args.headless:
        settings["headless"] = True
    if args.show_browser:
        settings["headless"] = False
    dirs = {"out": resolve_dir(settings["output_dir"]), "diag": resolve_dir(settings["diagnostics_dir"])}
    cleanup_diagnostics(dirs["diag"], settings["diagnostics_keep_days"])

    products = []
    if not args.login_only:
        if args.product_url:
            from request_flow import validate_product_url
            try:
                product_url = validate_product_url(args.product_url)
            except ValueError as exc:
                log.error("Invalid --product-url: %s", exc)
                return 1
            products = [(product_url, "")]
        else:
            csv_path = Path(settings["urls_csv"])
            csv_path = csv_path if csv_path.is_absolute() else BASE_DIR / csv_path
            try:
                products = load_products(csv_path)
            except Exception as exc:
                log.error("Cannot read %s: %s. For one product, pass --product-url <Flipkart product URL>; "
                          "for bulk scraping, restore the configured CSV.", csv_path, exc)
                return 1
            if args.only:
                products = [p for p in products if args.only.lower() in p[1].lower()]
            if args.limit:
                products = products[: args.limit]
        if not products:
            log.error("No matching product URLs in %s", csv_path)
            return 1

    driver, login = None, None
    results = []
    try:
        driver, login = start_session(settings, args.no_login, fresh_login=args.fresh_login)  # login happens BEFORE any product
        if args.login_only:
            write_report(results, login, settings)
            return 0 if login.logged_in else 1
        if login_required and not login.logged_in:
            log.error("A signed-in account is required but login did not succeed (%s) - no products processed.", login.status)
            write_report(results, login, settings)
            return 1
        if not login.logged_in:
            log.info("Continuing WITHOUT login: only publicly visible reviews will be collected.")
        for i, (url, name) in enumerate(products, 1):
            if not name:
                try:
                    from product_resolver import resolve_product
                    name, url = resolve_product(driver, url, settings.get("page_wait_seconds", 25))
                except Exception as exc:
                    log.error("Could not resolve the supplied Flipkart product URL (%s)", type(exc).__name__)
                    results.append({"product": "supplied product", "status": "navigation_failed",
                                    "failure_reason": "could not resolve the supplied Flipkart product URL",
                                    "warnings": []})
                    continue
            log.info("(%d/%d) %s", i, len(products), name)
            if not build_review_url(url):
                log.warning("URL shape not recognised, will use product-page fallback: %s", url[:80])
            res = process_product(driver, url, name, settings, selectors, dirs)
            if res["status"] in ("error", "navigation_failed"):  # browser may have died -> restart once and retry
                try:
                    driver.title  # raises if dead
                except Exception:
                    log.warning("Browser died - restarting and signing in again")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver, login = start_session(settings, args.no_login, fresh_login=args.fresh_login)
                    if login_required and not login.logged_in:
                        log.error("A signed-in account is required and re-login failed - stopping.")
                        results.append(res)
                        break
                    res = process_product(driver, url, name, settings, selectors, dirs)
            results.append(res)
    except Exception as exc:
        log.exception("Fatal error: %s", exc)
        if not results:
            return 1
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    write_report(results, login, settings)
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    log.info("Done: %s", ", ".join(f"{counts[k]} {k}" for k in
                                   ("success", "partial", "no_reviews", "navigation_failed", "blocked", "error")))
    send_alert(results)
    return 0 if all(r["status"] == "success" for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
