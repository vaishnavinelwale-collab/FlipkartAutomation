# v6
"""
Date-range tests (START_DATE..END_DATE, both inclusive). Offline only: FakePage + saved fixtures + temp workbooks.

Flipkart shows relative dates ('2 days ago', '3 months ago') or month-year ('Jan, 2026'), so most page-level
tests use those; absolute-day boundaries are tested on the date model and on workbook rows.
"""
import calendar
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

import fk_browser  # noqa: E402
import Flipkart_Reviews_Hearables as main  # noqa: E402
from fakes import FakePage, fixture, review_html, summary_html  # noqa: E402
from fk_collect import collect_reviews, scrape_product  # noqa: E402
from fk_common import BASE_DIR, DEFAULT_SELECTORS, DEFAULT_SETTINGS, resolve_date_range  # noqa: E402
from fk_excel import REVIEW_HEADERS, update_workbook  # noqa: E402
from fk_parsing import (_record, parse_iso_date, parse_review_date, parse_reviews_detailed,  # noqa: E402
                        review_window_state, window_state)

TODAY = date(2026, 10, 21)
START, END = date(2026, 1, 1), date(2026, 10, 21)
SEL = DEFAULT_SELECTORS
SET = {**DEFAULT_SETTINGS, "start_date": "2026-01-01", "end_date": "2026-10-21", "max_reviews": 1000,
       "max_scroll_rounds": 200}
URL = "https://www.flipkart.com/x/p/itm1?pid=A"


def month_text(m, y=2026):
    return f"{calendar.month_abbr[m]}, {y}"


def run(page, today=TODAY, **over):
    return scrape_product(page, "Prod", URL, {**SET, **over}, SEL, today=today)


def in_window(r, state=("in",)):
    return review_window_state(r, START, END) in state


# =========================================================================== date model
def test_parse_review_date_exact_relative_and_unknown():
    d = parse_review_date("· 2 days ago", TODAY)
    assert (d["earliest"], d["latest"], d["precision"], d["display"]) == (date(2026, 10, 19),) * 2 + ("day", "Oct 19, 2026")
    assert parse_review_date("today", TODAY)["earliest"] == TODAY
    assert parse_review_date("yesterday", TODAY)["earliest"] == date(2026, 10, 20)
    assert parse_review_date("Jan 05, 2026", TODAY)["earliest"] == date(2026, 1, 5)
    assert parse_review_date("2026-01-01", TODAY)["precision"] == "day"
    for junk in ("", "N/A", None, "soon", "· "):
        u = parse_review_date(junk, TODAY)
        assert u["earliest"] is None and u["latest"] is None and u["precision"] == "none"


def test_weeks_months_years_are_ranges_never_a_single_invented_day():
    wk = parse_review_date("2 weeks ago", TODAY)
    assert (wk["earliest"], wk["latest"], wk["precision"]) == (date(2026, 10, 1), date(2026, 10, 7), "week")
    mo = parse_review_date("3 months ago", TODAY)
    assert mo["precision"] == "month" and mo["earliest"] < mo["latest"] and mo["display"] == "3 months ago"
    assert mo["earliest"] == date(2026, 6, 22) and mo["latest"] == date(2026, 7, 21)
    yr = parse_review_date("1 year ago", TODAY)
    assert yr["precision"] == "year" and (yr["earliest"], yr["latest"]) == (date(2025, 9, 22), date(2025, 10, 21))
    my = parse_review_date("Feb, 2026", TODAY)
    assert (my["earliest"], my["latest"], my["display"]) == (date(2026, 2, 1), date(2026, 2, 28), "Feb, 2026")
    assert parse_review_date("Feb, 2024", TODAY)["latest"] == date(2024, 2, 29)       # leap year


def test_month_end_clamping_for_relative_months():
    d = parse_review_date("1 month ago", date(2026, 3, 31))
    assert d["latest"] == date(2026, 2, 28) and d["earliest"] == date(2026, 2, 1)


# =========================================================================== boundaries
def test_boundary_days_are_inclusive():
    def st(d):
        return window_state(d, d, START, END)
    assert st(date(2026, 1, 1)) == "in"            # start day included
    assert st(date(2025, 12, 31)) == "before"      # day before start excluded
    assert st(date(2026, 10, 21)) == "in"          # end day included
    assert st(date(2026, 10, 22)) == "after"       # day after end excluded
    assert window_state(None, None, START, END) == "undated"


def test_boundary_days_through_the_collector():
    # today = Jan 11 2026: '10 days ago' is exactly Jan 1 (kept), '11 days ago' is Dec 31 (older)
    texts = {0: "10 days ago", 1: "11 days ago", 2: "today"}
    res, data = run(FakePage(total=3, date_fn=texts.get), today=date(2026, 1, 11))
    assert sorted(r["Date"] for r in data["reviews"]) == ["Jan 01, 2026", "Jan 11, 2026"]
    assert res["reviews_before_range"] == 1 and res["reviews_in_range"] == 2
    # today = Oct 22 2026: '1 day ago' is exactly Oct 21 (kept), 'today' is Oct 22 (after the end date)
    texts = {0: "1 day ago", 1: "today", 2: "2 days ago"}
    res, data = run(FakePage(total=3, date_fn=texts.get), today=date(2026, 10, 22))
    assert sorted(r["Date"] for r in data["reviews"]) == ["Oct 20, 2026", "Oct 21, 2026"]
    assert res["reviews_after_range"] == 1


def test_approximate_month_dates_inside_range_are_kept_and_boundary_overlaps_follow_the_option():
    # Window is 2026-01-01..2026-10-21. 'Jan, 2026' lies entirely inside, 'Oct, 2026' overlaps the end
    # boundary (uncertain), 'Dec, 2025' is entirely before the start.
    texts = {0: month_text(1), 1: month_text(10), 2: month_text(12, 2025)}
    res, data = run(FakePage(total=3, date_fn=texts.get))              # include_uncertain_dates defaults to False
    assert [r["Date"] for r in data["reviews"]] == ["Jan, 2026"]
    assert "Approximate (month)" in data["reviews"][0]["Date Status"]
    assert res["reviews_in_range"] == 1 and res["reviews_date_uncertain"] == 1 and res["reviews_before_range"] == 1
    res2, data2 = run(FakePage(total=3, date_fn=texts.get), include_uncertain_dates=True)
    assert [r["Date"] for r in data2["reviews"]] == ["Jan, 2026", "Oct, 2026"]   # boundary overlap kept, flagged
    assert data2["reviews"][1]["Date Status"].startswith("Uncertain")
    assert res2["reviews_date_uncertain"] == 1 and res2["reviews_before_range"] == 1


def test_relative_month_dates_fully_inside_the_range_are_kept_as_approximate():
    # '3 months ago' (today Oct 21) = Jun 22..Jul 21 2026, entirely inside Jan 1..Oct 21 -> kept, labelled approximate
    res, data = run(FakePage(total=3, date_fn=lambda i: "3 months ago"))
    assert len(data["reviews"]) == 3
    assert all(r["Date"] == "3 months ago" and "Approximate (month)" in r["Date Status"] for r in data["reviews"])
    assert res["reviews_in_range"] == 3 and res["reviews_date_uncertain"] == 0


def test_requested_sep_2026_boundary_days_are_inclusive_and_adjacent_days_excluded():
    dates = {
        0: "Sep 01, 2026",
        1: "Sep 22, 2026",
        2: "Aug 31, 2026",
        3: "Sep 23, 2026",
    }
    res, data = run(
        FakePage(total=4, date_fn=dates.get),
        start_date="2026-09-01", end_date="2026-09-22",
        today=date(2026, 11, 6),
    )
    assert [row["User Name"] for row in data["reviews"]] == ["User 0", "User 1"]
    assert res["reviews_in_range"] == 2
    assert res["reviews_before_range"] == 1 and res["reviews_after_range"] == 1


# =========================================================================== missing dates
def test_missing_or_unreadable_dates_are_never_invented_or_listed():
    rec = _record("Solo", 4, "t", "some text", "", today=TODAY)
    assert rec["Date"] == "N/A" and rec["Date Earliest"] == "" and rec["Date Status"] == "Unknown date"
    assert review_window_state(rec, START, END) == "undated"
    rec2 = _record("Odd", 4, "t", "some text", "sometime soon", today=TODAY)
    assert rec2["Date"] == "sometime soon" and review_window_state(rec2, START, END) == "undated"
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        stats = {}
        update_workbook(p, "P", "2026-10-21", {}, None, [], [], [rec, rec2], stats, date_range=(START, END))
        assert stats["new_out_of_range"] == 2
        assert not [r for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]


# =========================================================================== pagination
def newest_first(i):
    """45 exact in-range dates from Oct 15 back to Sep 01, then older dates."""
    if i < 45:
        return (date(2026, 10, 15) - timedelta(days=i)).strftime("%b %d, %Y")
    return "Dec 15, 2025" if i < 80 else "Nov 15, 2025"


def test_scrolls_past_the_first_page_and_stops_at_the_start_date():
    page = FakePage(total=120, date_fn=newest_first)
    res, data = run(page)
    assert res["reviews_in_range"] == 45 == len(data["reviews"]) > 10           # not just the first page
    assert all(in_window(r, ("in",)) for r in data["reviews"])
    assert res["stop_reason"] == "reached_start_date" and res["coverage"] == "complete_to_start_date"
    assert res["reviews_scanned"] < 120 and page.scrolls < 12                   # did not crawl the whole history
    assert res["status"] == "success" and res["pagination_mode"] == "infinite_scroll"
    assert res["reviews_before_range"] == res["reviews_scanned"] - 45
    assert res["latest_sort_clicked"] is False                                  # order already confirmed newest-first


def test_page_number_fallback_is_not_used_after_scroll_stalls():
    class PageQueryTrap(FakePage):
        def goto_page(self, url):
            pytest.fail("unsupported ?page=N navigation must not be used")

        def select_sort_order(self, sort_order):
            pytest.fail("unsupported page fallback must not change sort order")

    page = PageQueryTrap(total=30, mode="pages", date_fn=lambda i: "Sep 10, 2026")
    res, data = run(page, start_date="2026-09-01", end_date="2026-09-22", scroll_patience=2)
    assert res["pagination_mode"] == "infinite_scroll"
    assert len(data["reviews"]) == 10
    assert res["stop_reason"] == "no_more_reviews_loaded"
    assert res["coverage"] == "stopped_unverified" and res["status"] == "partial"


def test_scroll_recovers_after_temporary_no_progress_and_reaches_date_boundary():
    class DelayedScroll(FakePage):
        def scroll_down(self):
            if self.scrolls < 2:
                self.scrolls += 1
                return
            super().scroll_down()

    page = DelayedScroll(total=60, date_fn=newest_first)
    res, data = run(page, start_date="2026-01-01", end_date="2026-10-21", scroll_patience=5)
    assert page.scrolls > 2
    assert res["reviews_in_range"] == 45 and len(data["reviews"]) == 45
    assert res["stop_reason"] == "reached_start_date"
    assert res["coverage"] == "complete_to_start_date"


def test_stalled_scroll_is_partial_and_never_claims_page_exhaustion():
    page = FakePage(total=25, mode="pages", date_fn=lambda i: "Jun 10, 2026")
    res, data = run(page, start_date="2026-01-01", end_date="2026-10-21", scroll_patience=2)
    assert len(data["reviews"]) == 10
    assert res["stop_reason"] == "no_more_reviews_loaded"
    assert res["coverage"] == "stopped_unverified" and res["status"] == "partial"
    assert res["pagination_mode"] == "infinite_scroll"


def test_latest_is_selected_when_order_is_not_newest_first():
    class Recording(FakePage):
        clicked = 0

        def click_latest(self):
            Recording.clicked += 1
            return True
    oldest_first = lambda i: month_text(1 + i // 5)                             # noqa: E731  Jan.. ascending
    res, _ = run(Recording(total=30, date_fn=oldest_first))
    assert Recording.clicked == 1 and res["latest_sort_clicked"] is True


def test_list_ends_before_start_date_is_reported_as_unverified_not_complete():
    res, data = run(FakePage(total=25, date_fn=lambda i: "Jun 10, 2026"))     # 25 exact dates in range, list just ends
    assert res["reviews_in_range"] == 25 and res["stop_reason"] == "no_more_reviews_loaded"
    assert res["coverage"] == "stopped_unverified"
    assert res["status"] == "partial" and "may be incomplete" in res["failure_reason"]
    assert any("could not confirm" in n for n in res["notes"])


def test_list_ending_right_after_older_reviews_counts_as_complete():
    res, _ = run(FakePage(total=25, date_fn=lambda i: "Jun 10, 2026" if i < 20 else "Dec 10, 2025"))
    assert res["reviews_in_range"] == 20 and res["reviews_before_range"] == 5
    assert res["coverage"] == "complete_to_start_date"


@pytest.mark.parametrize("over,limit", [({"max_reviews": 15}, "max_reviews"), ({"max_scroll_rounds": 2}, "max_scroll_rounds")])
def test_safety_limits_are_reported_and_make_the_run_partial(over, limit):
    res, data = run(FakePage(total=200, date_fn=lambda i: month_text(6)), **over)
    assert res["stop_reason"] == limit and res["coverage"] == "limit_reached"
    assert res["status"] == "partial" and limit in res["failure_reason"] and "may be missing" in res["failure_reason"]


def test_legacy_max_pages_setting_does_not_reenable_numbered_url_pagination():
    page = FakePage(total=25, mode="pages", date_fn=lambda i: "Jun 10, 2026")
    res, _ = run(page, max_pages=3, start_date="2026-01-01", end_date="2026-10-21", scroll_patience=2)
    assert res["stop_reason"] == "no_more_reviews_loaded"
    assert res["coverage"] == "stopped_unverified" and res["status"] == "partial"


# =========================================================================== order problems
def test_an_older_review_after_newer_ones_does_not_end_the_scan_or_get_listed():
    old_at = 15
    page = FakePage(total=40, date_fn=lambda i: "Dec 15, 2025" if i == old_at else "Jun 15, 2026")
    res, data = run(page)
    names = {r["User Name"] for r in data["reviews"]}
    assert len(data["reviews"]) == 39 and f"User {old_at}" not in names
    assert {"User 16", "User 39"} <= names                                      # reviews after the stray old one are kept
    assert res["reviews_before_range"] == 1 and res["reviews_scanned"] == 40


def test_a_run_of_older_reviews_in_an_unordered_list_needs_a_larger_streak():
    # in x10, OLD x10, in x10, OLD...: default streak (10) stops at the first old run; a larger one continues
    def pattern(i):
        return "Dec 15, 2025" if (10 <= i < 20 or i >= 30) else "Jun 15, 2026"
    res, _ = run(FakePage(total=90, date_fn=pattern))
    assert res["stop_reason"] == "reached_start_date" and res["reviews_in_range"] == 10
    res, _ = run(FakePage(total=90, date_fn=pattern), date_stop_streak=15)
    assert res["reviews_in_range"] == 20 and res["stop_reason"] == "reached_start_date"


# =========================================================================== duplicates
def test_duplicate_review_records_are_preserved_in_scroll_views():
    res, data = run(FakePage(total=60, ident_fn=lambda i: i % 30, date_fn=lambda i: "Jun 10, 2026"))
    assert len(data["reviews"]) == 60
    assert len({r["User Name"] for r in data["reviews"]}) == 30


def test_excel_preserves_duplicate_records_within_one_collection(tmp_path):
    from openpyxl import load_workbook
    from fk_excel import update_workbook

    record = {**_record("Same", 5, "Same title", "Same review text", "Mar 10, 2026", today=TODAY),
              "Reason": "R", "Consolidated Reason": "R"}
    path = str(tmp_path / "dupes.xlsx")
    update_workbook(path, "P", "2026-10-21", {}, None, [], [], [record, dict(record)],
                    date_range=(START, END))
    rows = [r for r in load_workbook(path)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]
    assert len(rows) == 2 and rows[0][:5] == rows[1][:5]


def test_duplicates_across_runs_do_not_repeat_in_excel_and_range_applies_to_old_rows():
    mk = lambda name, when: {**_record(name, 5, "t " + name, "text " + name, when, today=TODAY), "Reason": "x"}  # noqa: E731
    revs = [mk("A", "Mar 10, 2026"), mk("B", "Apr 10, 2026")]
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        for _ in range(2):
            update_workbook(p, "P", "2026-10-21", {}, None, [], [], revs, date_range=(START, END))
        rows = [r for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]
        assert [r[0] for r in rows] == ["A", "B"]
        # a later run with a narrower window removes the rows that no longer qualify
        stats = {}
        update_workbook(p, "P", "2026-10-22", {}, None, [], [], [], stats, date_range=(date(2026, 4, 1), END))
        rows = [r for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]
        assert [r[0] for r in rows] == ["B"] and stats["out_of_range_removed"] == 1


# =========================================================================== Excel output
def _recs(window_end=date(2026, 10, 15)):
    mk = lambda name, rating, when: {**_record(name, rating, "t " + name, "text " + name, when, "Color Red",  # noqa: E731
                                              "Pune", 3, True, today=TODAY), "Reason": "R", "Consolidated Reason": "R"}
    return [mk("exact_in", 5, "Oct 11, 2026"), mk("month_in", 4, "Mar 10, 2026"), mk("straddle", 5, "Oct, 2026"),
            mk("old", 5, "Dec 10, 2025"), mk("last_year", 1, "1 year ago"), mk("future", 5, "Oct 19, 2026"),
            mk("neg_in", 1, "Feb 10, 2026"), mk("neg_old", 2, "Nov 10, 2025"),
            {"User Name": "nodate", "Ratings": 5, "Short Review": "x", "Review Description": "y"}]


def test_excel_review_sheets_contain_only_in_range_records_and_show_the_range():
    recs = _recs()
    window = (START, date(2026, 10, 15))
    neg, pos = main.build_negative_df(recs, {1, 2, 3}), main.build_positive_df(recs, {1, 2, 3})
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        stats = {}
        problems, _ = update_workbook(p, "Prod", "2026-10-21", {5: 100, 4: 10, 3: 1, 2: 1, 1: 1}, 4.5, [], neg, pos, stats,
                                      date_range=window, run_info={"scraped_at": "2026-10-21 15:30:00", "coverage": "complete_to_start_date",
                                                                   "stop_reason": "reached_start_date"})
        assert problems == []
        wb = load_workbook(p)
        rows = lambda n: [dict(zip(REVIEW_HEADERS, r)) for r in wb[n].iter_rows(min_row=5, max_col=len(REVIEW_HEADERS), values_only=True) if r[0]]  # noqa: E731
        pos_rows, neg_rows = rows("Positive Reviews"), rows("Negative Reviews")
        assert [r["User Name"] for r in pos_rows] == ["exact_in", "month_in"]
        assert [r["User Name"] for r in neg_rows] == ["neg_in"]
        assert stats["new_out_of_range"] == 6  # straddle, old, last_year, future, neg_old, nodate
        by = {r["User Name"]: r for r in pos_rows}
        assert by["exact_in"]["Date"] == "Oct 11, 2026" and by["exact_in"]["Date Status"] == "Exact"
        assert by["exact_in"]["Possible Date Range"] == "2026-10-11"
        assert by["month_in"]["Date"] == "Mar 10, 2026" and by["month_in"]["Date Status"] == "Exact"
        assert "straddle" not in by
        # metadata
        for sheet in ("Positive Reviews", "Negative Reviews"):
            assert "2026-01-01 to 2026-10-15 (inclusive)" in wb[sheet]["A3"].value
            assert [c.value for c in wb[sheet][4]][:len(REVIEW_HEADERS)] == REVIEW_HEADERS
        assert "2026-01-01 to 2026-10-15" in wb["Star Ratings"]["A3"].value and "all time" in wb["Star Ratings"]["A3"].value
        t = [c.value for c in wb["Time"][2]]
        assert t[0] == "N/A" and t[4:6] == ["2026-01-01", "2026-10-15"]
        assert t[6] == "2026-10-21 15:30:00" and t[11:13] == ["complete_to_start_date", "reached_start_date"]
        assert [c.value for c in wb["Time"][1]][:6] == ["Request ID", "Product Name", "Requested Duration", "Duration Unit", "Start Date", "End Date"]
        # aggregates are untouched by the date filter and never create rows
        assert wb["Star Ratings"]["B5"].value == 100 and len(pos_rows) == 2 < 100


def test_review_date_and_scraping_timestamp_are_separate_fields():
    r = {**_record("A", 5, "t", "x", "10 days ago", today=TODAY), "Scraped At": "2026-10-21 15:30:00"}
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        update_workbook(p, "P", "2026-10-21", {}, None, [], [], [r], date_range=(START, END))
        row = dict(zip(REVIEW_HEADERS, next(load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, max_col=15, values_only=True))))
        assert row["Date"] == "Oct 11, 2026"                       # review date
        assert row["Scraped At"] == "2026-10-21 15:30:00" and row["Run Date"] == "2026-10-21"
        assert row["Date"] != row["Scraped At"]
    # a review without a date never borrows the scraping time
    rec = _record("A", 5, "t", "x", "", today=TODAY)
    assert rec["Date"] == "N/A" and "Scraped At" not in rec


def test_absolute_dates_at_the_boundaries_in_sheet_rows():
    base = {"Ratings": 5, "Short Review": "t", "Review Description": "d"}
    revs = [{**base, "User Name": n, "Date": dt} for n, dt in
            [("start", "Jan 01, 2026"), ("before", "Dec 31, 2025"), ("end", "Oct 21, 2026"), ("after", "Oct 22, 2026")]]
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        update_workbook(p, "P", "2026-10-21", {}, None, [], [], revs, date_range=(START, END))
        names = [r[0] for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]
        assert names == ["start", "end"]


def test_rows_from_older_workbooks_are_filtered_by_their_stored_date_and_marked_legacy():
    base = {"Ratings": 5, "Short Review": "t", "Review Description": "d"}
    old = [{**base, "User Name": "in2026", "Date": "Mar 10, 2026"}, {**base, "User Name": "in2025", "Date": "Jan 05, 2025"},
           {**base, "User Name": "nodate"}]
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        update_workbook(p, "P", "2026-10-01", {}, None, [], [], old)                       # no filter: older behaviour
        wb = load_workbook(p)
        for r in list(wb["Positive Reviews"].iter_rows(min_row=5)):                          # simulate pre-upgrade layout
            for c in r[12:15]:
                c.value = None
        wb.save(p)
        stats = {}
        update_workbook(p, "P", "2026-10-21", {}, None, [], [], [], stats, date_range=(START, END))
        rows = [r for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, max_col=15, values_only=True) if r[0]]
        assert [r[0] for r in rows] == ["in2026"] and stats["out_of_range_removed"] == 2
        assert rows[0][12] == "Legacy row - day may be estimated" and rows[0][13] == "N/A"


def test_aggregate_counts_still_never_create_records_with_date_filter():
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        update_workbook(p, "P", "2026-10-21", {5: 18587, 4: 6271, 3: 2511, 2: 1264, 1: 3002}, 4.2, [], [], [],
                        date_range=(START, END))
        wb = load_workbook(p)
        for n in ("Positive Reviews", "Negative Reviews"):
            assert not [r for r in wb[n].iter_rows(min_row=5, max_col=1, values_only=True) if r[0]]


# =========================================================================== config / CLI
def test_config_file_carries_the_requested_range_and_env_and_cli_override_it(monkeypatch):
    cfg = json.loads((BASE_DIR / "fk_config.json").read_text(encoding="utf-8"))["settings"]
    assert (cfg["start_date"], cfg["end_date"]) == ("", "")
    monkeypatch.delenv("START_DATE", raising=False)
    monkeypatch.delenv("END_DATE", raising=False)
    assert resolve_date_range(cfg) == (None, None)
    monkeypatch.setenv("START_DATE", "2026-03-01")
    monkeypatch.setenv("END_DATE", "2026-04-30")
    assert resolve_date_range(cfg) == (date(2026, 3, 1), date(2026, 4, 30))               # env beats config
    assert resolve_date_range(cfg, "2026-02-01", None) == (date(2026, 2, 1), date(2026, 4, 30))   # CLI beats env
    assert resolve_date_range({}) == (date(2026, 3, 1), date(2026, 4, 30))
    monkeypatch.delenv("START_DATE")
    monkeypatch.delenv("END_DATE")
    assert resolve_date_range({}) == (None, None)


def test_bad_dates_are_rejected(monkeypatch):
    monkeypatch.delenv("START_DATE", raising=False)
    monkeypatch.delenv("END_DATE", raising=False)
    for bad in ("01-01-2026", "2026-13-01", "yesterday"):
        with pytest.raises(ValueError):
            parse_iso_date(bad)
    with pytest.raises(ValueError):
        resolve_date_range({"start_date": "2026-10-22", "end_date": "2026-10-21"})
    assert main.main(["--start-date", "not-a-date"]) == 1                                # exits before any browser starts
    assert main.main(["--start-date", "2026-10-22", "--end-date", "2026-10-21"]) == 1


def test_without_dates_nothing_is_filtered():
    res, data = scrape_product(FakePage(total=12), "P", URL, {**DEFAULT_SETTINGS}, SEL)
    assert res["valid_reviews"] == 12 and res["coverage"] == "not_date_filtered"
    assert res["reviews_before_range"] == 0


# =========================================================================== end to end
def test_process_product_end_to_end_with_date_range(monkeypatch):
    today = date.today()
    # reviews: 0..4 recent (in range), 5.. much older (month-year a year back)
    older = f"{calendar.month_abbr[today.month]} {today.day:02d}, {today.year - 2}"
    date_fn = lambda i: f"{i + 1} days ago" if i < 5 else older                           # noqa: E731
    with tempfile.TemporaryDirectory() as d:
        dirs = {"out": Path(d), "diag": Path(d)}
        monkeypatch.setattr(fk_browser, "LivePage", lambda drv, st, diag: FakePage(total=40, date_fn=date_fn))
        settings = {**DEFAULT_SETTINGS, "keyword_feedback_enabled": False, "max_reviews": 1000, "max_scroll_rounds": 200,
                    "start_date": (today - timedelta(days=30)).isoformat(), "end_date": today.isoformat()}
        res = main.process_product(None, URL, "Boat: Range", settings, SEL, dirs)
        assert res["status"] == "success", res
        assert res["reviews_in_range"] == 5 == res["valid_reviews"] and res["coverage"] == "complete_to_start_date"
        assert res["positive_reviews"] + res["negative_reviews"] == 5
        assert res["date_start"] == settings["start_date"] and res["date_end"] == settings["end_date"]
        wb = load_workbook(Path(d) / "Boat Range.xlsx")
        rows = [dict(zip(REVIEW_HEADERS, r)) for n in ("Positive Reviews", "Negative Reviews")
                for r in wb[n].iter_rows(min_row=5, max_col=15, values_only=True) if r[0]]
        assert len(rows) == 5 and all(r["Scraped At"] not in (None, "N/A") for r in rows)
        assert all(r["Date"] != r["Scraped At"] for r in rows)
        assert wb["Star Ratings"]["B5"].value == 18587                                      # aggregates untouched, no padding
        assert any("date window" in n for n in res["notes"])


def test_real_fixture_parses_date_fields():
    d = parse_reviews_detailed(fixture("reviews_airdopes800.html"), SEL, today=TODAY)
    assert len(d["reviews"]) == 10
    for r in d["reviews"]:
        assert r["Date Precision"] in ("day", "week", "month", "year") and r["Date Latest"] and r["Date Status"]





# """
# Date-range tests (START_DATE..END_DATE, both inclusive). Offline only: FakePage + saved fixtures + temp workbooks.

# Flipkart shows relative dates ('2 days ago', '3 months ago') or month-year ('Jan, 2026'), so most page-level
# tests use those; absolute-day boundaries are tested on the date model and on workbook rows.
# """
# import calendar
# import json
# import sys
# import tempfile
# from datetime import date, datetime, timedelta
# from pathlib import Path

# sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# sys.path.insert(0, str(Path(__file__).resolve().parent))

# import pytest  # noqa: E402
# from openpyxl import load_workbook  # noqa: E402

# import fk_browser  # noqa: E402
# import Flipkart_Reviews_Hearables as main  # noqa: E402
# from fakes import FakePage, fixture, review_html, summary_html  # noqa: E402
# from fk_collect import collect_reviews, scrape_product  # noqa: E402
# from fk_common import BASE_DIR, DEFAULT_SELECTORS, DEFAULT_SETTINGS, resolve_date_range  # noqa: E402
# from fk_excel import REVIEW_HEADERS, update_workbook  # noqa: E402
# from fk_parsing import (_record, parse_iso_date, parse_review_date, parse_reviews_detailed,  # noqa: E402
#                         review_window_state, window_state)

# TODAY = date(2026, 10, 21)
# START, END = date(2026, 1, 1), date(2026, 10, 21)
# SEL = DEFAULT_SELECTORS
# SET = {**DEFAULT_SETTINGS, "start_date": "2026-01-01", "end_date": "2026-10-21", "max_reviews": 1000,
#        "max_scroll_rounds": 200}
# URL = "https://www.flipkart.com/x/p/itm1?pid=A"


# def month_text(m, y=2026):
#     return f"{calendar.month_abbr[m]}, {y}"


# def run(page, today=TODAY, **over):
#     return scrape_product(page, "Prod", URL, {**SET, **over}, SEL, today=today)


# def in_window(r, state=("in",)):
#     return review_window_state(r, START, END) in state


# # =========================================================================== date model
# def test_parse_review_date_exact_relative_and_unknown():
#     d = parse_review_date("· 2 days ago", TODAY)
#     assert (d["earliest"], d["latest"], d["precision"], d["display"]) == (date(2026, 10, 19),) * 2 + ("day", "Oct 19, 2026")
#     assert parse_review_date("today", TODAY)["earliest"] == TODAY
#     assert parse_review_date("yesterday", TODAY)["earliest"] == date(2026, 10, 20)
#     assert parse_review_date("Jan 05, 2026", TODAY)["earliest"] == date(2026, 1, 5)
#     assert parse_review_date("2026-01-01", TODAY)["precision"] == "day"
#     for junk in ("", "N/A", None, "soon", "· "):
#         u = parse_review_date(junk, TODAY)
#         assert u["earliest"] is None and u["latest"] is None and u["precision"] == "none"


# def test_weeks_months_years_are_ranges_never_a_single_invented_day():
#     wk = parse_review_date("2 weeks ago", TODAY)
#     assert (wk["earliest"], wk["latest"], wk["precision"]) == (date(2026, 10, 1), date(2026, 10, 7), "week")
#     mo = parse_review_date("3 months ago", TODAY)
#     assert mo["precision"] == "month" and mo["earliest"] < mo["latest"] and mo["display"] == "3 months ago"
#     assert mo["earliest"] == date(2026, 6, 22) and mo["latest"] == date(2026, 7, 21)
#     yr = parse_review_date("1 year ago", TODAY)
#     assert yr["precision"] == "year" and (yr["earliest"], yr["latest"]) == (date(2025, 9, 22), date(2025, 10, 21))
#     my = parse_review_date("Feb, 2026", TODAY)
#     assert (my["earliest"], my["latest"], my["display"]) == (date(2026, 2, 1), date(2026, 2, 28), "Feb, 2026")
#     assert parse_review_date("Feb, 2024", TODAY)["latest"] == date(2024, 2, 29)       # leap year


# def test_month_end_clamping_for_relative_months():
#     d = parse_review_date("1 month ago", date(2026, 3, 31))
#     assert d["latest"] == date(2026, 2, 28) and d["earliest"] == date(2026, 2, 1)


# # =========================================================================== boundaries
# def test_boundary_days_are_inclusive():
#     def st(d):
#         return window_state(d, d, START, END)
#     assert st(date(2026, 1, 1)) == "in"            # start day included
#     assert st(date(2025, 12, 31)) == "before"      # day before start excluded
#     assert st(date(2026, 10, 21)) == "in"          # end day included
#     assert st(date(2026, 10, 22)) == "after"       # day after end excluded
#     assert window_state(None, None, START, END) == "undated"


# def test_boundary_days_through_the_collector():
#     # today = Jan 11 2026: '10 days ago' is exactly Jan 1 (kept), '11 days ago' is Dec 31 (older)
#     texts = {0: "10 days ago", 1: "11 days ago", 2: "today"}
#     res, data = run(FakePage(total=3, date_fn=texts.get), today=date(2026, 1, 11))
#     assert sorted(r["Date"] for r in data["reviews"]) == ["Jan 01, 2026", "Jan 11, 2026"]
#     assert res["reviews_before_range"] == 1 and res["reviews_in_range"] == 2
#     # today = Oct 22 2026: '1 day ago' is exactly Oct 21 (kept), 'today' is Oct 22 (after the end date)
#     texts = {0: "1 day ago", 1: "today", 2: "2 days ago"}
#     res, data = run(FakePage(total=3, date_fn=texts.get), today=date(2026, 10, 22))
#     assert sorted(r["Date"] for r in data["reviews"]) == ["Oct 20, 2026", "Oct 21, 2026"]
#     assert res["reviews_after_range"] == 1


# def test_uncertain_month_and_relative_dates_are_excluded_even_if_legacy_option_is_true():
#     texts = {0: month_text(1), 1: month_text(10), 2: month_text(12, 2025)}
#     res, data = run(FakePage(total=3, date_fn=texts.get))
#     assert data["reviews"] == []
#     assert res["reviews_date_uncertain"] == 2 and res["reviews_before_range"] == 1
#     res2, data2 = run(FakePage(total=3, date_fn=texts.get), include_uncertain_dates=True)
#     assert data2["reviews"] == []  # exact-day policy cannot be overridden by old config
#     assert res2["reviews_date_uncertain"] == 2


# def test_relative_month_dates_are_parsed_but_never_exported_to_a_strict_range():
#     res, data = run(FakePage(total=3, date_fn=lambda i: "3 months ago"))
#     assert data["reviews"] == []
#     assert res["reviews_date_uncertain"] == 3


# def test_requested_sep_2026_boundary_days_are_inclusive_and_adjacent_days_excluded():
#     dates = {
#         0: "Sep 01, 2026",
#         1: "Sep 22, 2026",
#         2: "Aug 31, 2026",
#         3: "Sep 23, 2026",
#     }
#     res, data = run(
#         FakePage(total=4, date_fn=dates.get),
#         start_date="2026-09-01", end_date="2026-09-22",
#         today=date(2026, 11, 6),
#     )
#     assert [row["User Name"] for row in data["reviews"]] == ["User 0", "User 1"]
#     assert res["reviews_in_range"] == 2
#     assert res["reviews_before_range"] == 1 and res["reviews_after_range"] == 1


# # =========================================================================== missing dates
# def test_missing_or_unreadable_dates_are_never_invented_or_listed():
#     rec = _record("Solo", 4, "t", "some text", "", today=TODAY)
#     assert rec["Date"] == "N/A" and rec["Date Earliest"] == "" and rec["Date Status"] == "Unknown date"
#     assert review_window_state(rec, START, END) == "undated"
#     rec2 = _record("Odd", 4, "t", "some text", "sometime soon", today=TODAY)
#     assert rec2["Date"] == "sometime soon" and review_window_state(rec2, START, END) == "undated"
#     with tempfile.TemporaryDirectory() as d:
#         p = str(Path(d) / "t.xlsx")
#         stats = {}
#         update_workbook(p, "P", "2026-10-21", {}, None, [], [], [rec, rec2], stats, date_range=(START, END))
#         assert stats["new_out_of_range"] == 2
#         assert not [r for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]


# # =========================================================================== pagination
# def newest_first(i):
#     """45 exact in-range dates from Oct 15 back to Sep 01, then older dates."""
#     if i < 45:
#         return (date(2026, 10, 15) - timedelta(days=i)).strftime("%b %d, %Y")
#     return "Dec 15, 2025" if i < 80 else "Nov 15, 2025"


# def test_scrolls_past_the_first_page_and_stops_at_the_start_date():
#     page = FakePage(total=120, date_fn=newest_first)
#     res, data = run(page)
#     assert res["reviews_in_range"] == 45 == len(data["reviews"]) > 10           # not just the first page
#     assert all(in_window(r, ("in",)) for r in data["reviews"])
#     assert res["stop_reason"] == "reached_start_date" and res["coverage"] == "complete_to_start_date"
#     assert res["reviews_scanned"] < 120 and page.scrolls < 12                   # did not crawl the whole history
#     assert res["status"] == "success" and res["pagination_mode"] == "infinite_scroll"
#     assert res["reviews_before_range"] == res["reviews_scanned"] - 45
#     assert res["latest_sort_clicked"] is False                                  # order already confirmed newest-first


# def test_page_number_fallback_is_not_used_after_scroll_stalls():
#     class PageQueryTrap(FakePage):
#         def goto_page(self, url):
#             pytest.fail("unsupported ?page=N navigation must not be used")

#         def select_sort_order(self, sort_order):
#             pytest.fail("unsupported page fallback must not change sort order")

#     page = PageQueryTrap(total=30, mode="pages", date_fn=lambda i: "Sep 10, 2026")
#     res, data = run(page, start_date="2026-09-01", end_date="2026-09-22", scroll_patience=2)
#     assert res["pagination_mode"] == "infinite_scroll"
#     assert len(data["reviews"]) == 10
#     assert res["stop_reason"] == "no_more_reviews_loaded"
#     assert res["coverage"] == "stopped_unverified" and res["status"] == "partial"


# def test_scroll_recovers_after_temporary_no_progress_and_reaches_date_boundary():
#     class DelayedScroll(FakePage):
#         def scroll_down(self):
#             if self.scrolls < 2:
#                 self.scrolls += 1
#                 return
#             super().scroll_down()

#     page = DelayedScroll(total=60, date_fn=newest_first)
#     res, data = run(page, start_date="2026-01-01", end_date="2026-10-21", scroll_patience=5)
#     assert page.scrolls > 2
#     assert res["reviews_in_range"] == 45 and len(data["reviews"]) == 45
#     assert res["stop_reason"] == "reached_start_date"
#     assert res["coverage"] == "complete_to_start_date"


# def test_stalled_scroll_is_partial_and_never_claims_page_exhaustion():
#     page = FakePage(total=25, mode="pages", date_fn=lambda i: "Jun 10, 2026")
#     res, data = run(page, start_date="2026-01-01", end_date="2026-10-21", scroll_patience=2)
#     assert len(data["reviews"]) == 10
#     assert res["stop_reason"] == "no_more_reviews_loaded"
#     assert res["coverage"] == "stopped_unverified" and res["status"] == "partial"
#     assert res["pagination_mode"] == "infinite_scroll"


# def test_latest_is_selected_when_order_is_not_newest_first():
#     class Recording(FakePage):
#         clicked = 0

#         def click_latest(self):
#             Recording.clicked += 1
#             return True
#     oldest_first = lambda i: month_text(1 + i // 5)                             # noqa: E731  Jan.. ascending
#     res, _ = run(Recording(total=30, date_fn=oldest_first))
#     assert Recording.clicked == 1 and res["latest_sort_clicked"] is True


# def test_list_ends_before_start_date_is_reported_as_unverified_not_complete():
#     res, data = run(FakePage(total=25, date_fn=lambda i: "Jun 10, 2026"))     # 25 exact dates in range, list just ends
#     assert res["reviews_in_range"] == 25 and res["stop_reason"] == "no_more_reviews_loaded"
#     assert res["coverage"] == "stopped_unverified"
#     assert res["status"] == "partial" and "may be incomplete" in res["failure_reason"]
#     assert any("could not confirm" in n for n in res["notes"])


# def test_list_ending_right_after_older_reviews_counts_as_complete():
#     res, _ = run(FakePage(total=25, date_fn=lambda i: "Jun 10, 2026" if i < 20 else "Dec 10, 2025"))
#     assert res["reviews_in_range"] == 20 and res["reviews_before_range"] == 5
#     assert res["coverage"] == "complete_to_start_date"


# @pytest.mark.parametrize("over,limit", [({"max_reviews": 15}, "max_reviews"), ({"max_scroll_rounds": 2}, "max_scroll_rounds")])
# def test_safety_limits_are_reported_and_make_the_run_partial(over, limit):
#     res, data = run(FakePage(total=200, date_fn=lambda i: month_text(6)), **over)
#     assert res["stop_reason"] == limit and res["coverage"] == "limit_reached"
#     assert res["status"] == "partial" and limit in res["failure_reason"] and "may be missing" in res["failure_reason"]


# def test_legacy_max_pages_setting_does_not_reenable_numbered_url_pagination():
#     page = FakePage(total=25, mode="pages", date_fn=lambda i: "Jun 10, 2026")
#     res, _ = run(page, max_pages=3, start_date="2026-01-01", end_date="2026-10-21", scroll_patience=2)
#     assert res["stop_reason"] == "no_more_reviews_loaded"
#     assert res["coverage"] == "stopped_unverified" and res["status"] == "partial"


# # =========================================================================== order problems
# def test_an_older_review_after_newer_ones_does_not_end_the_scan_or_get_listed():
#     old_at = 15
#     page = FakePage(total=40, date_fn=lambda i: "Dec 15, 2025" if i == old_at else "Jun 15, 2026")
#     res, data = run(page)
#     names = {r["User Name"] for r in data["reviews"]}
#     assert len(data["reviews"]) == 39 and f"User {old_at}" not in names
#     assert {"User 16", "User 39"} <= names                                      # reviews after the stray old one are kept
#     assert res["reviews_before_range"] == 1 and res["reviews_scanned"] == 40


# def test_a_run_of_older_reviews_in_an_unordered_list_needs_a_larger_streak():
#     # in x10, OLD x10, in x10, OLD...: default streak (10) stops at the first old run; a larger one continues
#     def pattern(i):
#         return "Dec 15, 2025" if (10 <= i < 20 or i >= 30) else "Jun 15, 2026"
#     res, _ = run(FakePage(total=90, date_fn=pattern))
#     assert res["stop_reason"] == "reached_start_date" and res["reviews_in_range"] == 10
#     res, _ = run(FakePage(total=90, date_fn=pattern), date_stop_streak=15)
#     assert res["reviews_in_range"] == 20 and res["stop_reason"] == "reached_start_date"


# # =========================================================================== duplicates
# def test_duplicate_review_records_are_preserved_in_scroll_views():
#     res, data = run(FakePage(total=60, ident_fn=lambda i: i % 30, date_fn=lambda i: "Jun 10, 2026"))
#     assert len(data["reviews"]) == 60
#     assert len({r["User Name"] for r in data["reviews"]}) == 30


# def test_excel_preserves_duplicate_records_within_one_collection(tmp_path):
#     from openpyxl import load_workbook
#     from fk_excel import update_workbook

#     record = {**_record("Same", 5, "Same title", "Same review text", "Mar 10, 2026", today=TODAY),
#               "Reason": "R", "Consolidated Reason": "R"}
#     path = str(tmp_path / "dupes.xlsx")
#     update_workbook(path, "P", "2026-10-21", {}, None, [], [], [record, dict(record)],
#                     date_range=(START, END))
#     rows = [r for r in load_workbook(path)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]
#     assert len(rows) == 2 and rows[0][:5] == rows[1][:5]


# def test_duplicates_across_runs_do_not_repeat_in_excel_and_range_applies_to_old_rows():
#     mk = lambda name, when: {**_record(name, 5, "t " + name, "text " + name, when, today=TODAY), "Reason": "x"}  # noqa: E731
#     revs = [mk("A", "Mar 10, 2026"), mk("B", "Apr 10, 2026")]
#     with tempfile.TemporaryDirectory() as d:
#         p = str(Path(d) / "t.xlsx")
#         for _ in range(2):
#             update_workbook(p, "P", "2026-10-21", {}, None, [], [], revs, date_range=(START, END))
#         rows = [r for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]
#         assert [r[0] for r in rows] == ["A", "B"]
#         # a later run with a narrower window removes the rows that no longer qualify
#         stats = {}
#         update_workbook(p, "P", "2026-10-22", {}, None, [], [], [], stats, date_range=(date(2026, 4, 1), END))
#         rows = [r for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]
#         assert [r[0] for r in rows] == ["B"] and stats["out_of_range_removed"] == 1


# # =========================================================================== Excel output
# def _recs(window_end=date(2026, 10, 15)):
#     mk = lambda name, rating, when: {**_record(name, rating, "t " + name, "text " + name, when, "Color Red",  # noqa: E731
#                                               "Pune", 3, True, today=TODAY), "Reason": "R", "Consolidated Reason": "R"}
#     return [mk("exact_in", 5, "Oct 11, 2026"), mk("month_in", 4, "Mar 10, 2026"), mk("straddle", 5, "Oct, 2026"),
#             mk("old", 5, "Dec 10, 2025"), mk("last_year", 1, "1 year ago"), mk("future", 5, "Oct 19, 2026"),
#             mk("neg_in", 1, "Feb 10, 2026"), mk("neg_old", 2, "Nov 10, 2025"),
#             {"User Name": "nodate", "Ratings": 5, "Short Review": "x", "Review Description": "y"}]


# def test_excel_review_sheets_contain_only_in_range_records_and_show_the_range():
#     recs = _recs()
#     window = (START, date(2026, 10, 15))
#     neg, pos = main.build_negative_df(recs, {1, 2, 3}), main.build_positive_df(recs, {1, 2, 3})
#     with tempfile.TemporaryDirectory() as d:
#         p = str(Path(d) / "t.xlsx")
#         stats = {}
#         problems, _ = update_workbook(p, "Prod", "2026-10-21", {5: 100, 4: 10, 3: 1, 2: 1, 1: 1}, 4.5, [], neg, pos, stats,
#                                       date_range=window, run_info={"scraped_at": "2026-10-21 15:30:00", "coverage": "complete_to_start_date",
#                                                                    "stop_reason": "reached_start_date"})
#         assert problems == []
#         wb = load_workbook(p)
#         rows = lambda n: [dict(zip(REVIEW_HEADERS, r)) for r in wb[n].iter_rows(min_row=5, max_col=len(REVIEW_HEADERS), values_only=True) if r[0]]  # noqa: E731
#         pos_rows, neg_rows = rows("Positive Reviews"), rows("Negative Reviews")
#         assert [r["User Name"] for r in pos_rows] == ["exact_in", "month_in"]
#         assert [r["User Name"] for r in neg_rows] == ["neg_in"]
#         assert stats["new_out_of_range"] == 6  # straddle, old, last_year, future, neg_old, nodate
#         by = {r["User Name"]: r for r in pos_rows}
#         assert by["exact_in"]["Date"] == "Oct 11, 2026" and by["exact_in"]["Date Status"] == "Exact"
#         assert by["exact_in"]["Possible Date Range"] == "2026-10-11"
#         assert by["month_in"]["Date"] == "Mar 10, 2026" and by["month_in"]["Date Status"] == "Exact"
#         assert "straddle" not in by
#         # metadata
#         for sheet in ("Positive Reviews", "Negative Reviews"):
#             assert "2026-01-01 to 2026-10-15 (inclusive)" in wb[sheet]["A3"].value
#             assert [c.value for c in wb[sheet][4]][:len(REVIEW_HEADERS)] == REVIEW_HEADERS
#         assert "2026-01-01 to 2026-10-15" in wb["Star Ratings"]["A3"].value and "all time" in wb["Star Ratings"]["A3"].value
#         t = [c.value for c in wb["Time"][2]]
#         assert t[0] == "N/A" and t[4:6] == ["2026-01-01", "2026-10-15"]
#         assert t[6] == "2026-10-21 15:30:00" and t[11:13] == ["complete_to_start_date", "reached_start_date"]
#         assert [c.value for c in wb["Time"][1]][:6] == ["Request ID", "Product Name", "Requested Duration", "Duration Unit", "Start Date", "End Date"]
#         # aggregates are untouched by the date filter and never create rows
#         assert wb["Star Ratings"]["B5"].value == 100 and len(pos_rows) == 2 < 100


# def test_review_date_and_scraping_timestamp_are_separate_fields():
#     r = {**_record("A", 5, "t", "x", "10 days ago", today=TODAY), "Scraped At": "2026-10-21 15:30:00"}
#     with tempfile.TemporaryDirectory() as d:
#         p = str(Path(d) / "t.xlsx")
#         update_workbook(p, "P", "2026-10-21", {}, None, [], [], [r], date_range=(START, END))
#         row = dict(zip(REVIEW_HEADERS, next(load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, max_col=15, values_only=True))))
#         assert row["Date"] == "Oct 11, 2026"                       # review date
#         assert row["Scraped At"] == "2026-10-21 15:30:00" and row["Run Date"] == "2026-10-21"
#         assert row["Date"] != row["Scraped At"]
#     # a review without a date never borrows the scraping time
#     rec = _record("A", 5, "t", "x", "", today=TODAY)
#     assert rec["Date"] == "N/A" and "Scraped At" not in rec


# def test_absolute_dates_at_the_boundaries_in_sheet_rows():
#     base = {"Ratings": 5, "Short Review": "t", "Review Description": "d"}
#     revs = [{**base, "User Name": n, "Date": dt} for n, dt in
#             [("start", "Jan 01, 2026"), ("before", "Dec 31, 2025"), ("end", "Oct 21, 2026"), ("after", "Oct 22, 2026")]]
#     with tempfile.TemporaryDirectory() as d:
#         p = str(Path(d) / "t.xlsx")
#         update_workbook(p, "P", "2026-10-21", {}, None, [], [], revs, date_range=(START, END))
#         names = [r[0] for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, values_only=True) if r[0]]
#         assert names == ["start", "end"]


# def test_rows_from_older_workbooks_are_filtered_by_their_stored_date_and_marked_legacy():
#     base = {"Ratings": 5, "Short Review": "t", "Review Description": "d"}
#     old = [{**base, "User Name": "in2026", "Date": "Mar 10, 2026"}, {**base, "User Name": "in2025", "Date": "Jan 05, 2025"},
#            {**base, "User Name": "nodate"}]
#     with tempfile.TemporaryDirectory() as d:
#         p = str(Path(d) / "t.xlsx")
#         update_workbook(p, "P", "2026-10-01", {}, None, [], [], old)                       # no filter: older behaviour
#         wb = load_workbook(p)
#         for r in list(wb["Positive Reviews"].iter_rows(min_row=5)):                          # simulate pre-upgrade layout
#             for c in r[12:15]:
#                 c.value = None
#         wb.save(p)
#         stats = {}
#         update_workbook(p, "P", "2026-10-21", {}, None, [], [], [], stats, date_range=(START, END))
#         rows = [r for r in load_workbook(p)["Positive Reviews"].iter_rows(min_row=5, max_col=15, values_only=True) if r[0]]
#         assert [r[0] for r in rows] == ["in2026"] and stats["out_of_range_removed"] == 2
#         assert rows[0][12] == "Legacy row - day may be estimated" and rows[0][13] == "N/A"


# def test_aggregate_counts_still_never_create_records_with_date_filter():
#     with tempfile.TemporaryDirectory() as d:
#         p = str(Path(d) / "t.xlsx")
#         update_workbook(p, "P", "2026-10-21", {5: 18587, 4: 6271, 3: 2511, 2: 1264, 1: 3002}, 4.2, [], [], [],
#                         date_range=(START, END))
#         wb = load_workbook(p)
#         for n in ("Positive Reviews", "Negative Reviews"):
#             assert not [r for r in wb[n].iter_rows(min_row=5, max_col=1, values_only=True) if r[0]]


# # =========================================================================== config / CLI
# def test_config_file_carries_the_requested_range_and_env_and_cli_override_it(monkeypatch):
#     cfg = json.loads((BASE_DIR / "fk_config.json").read_text(encoding="utf-8"))["settings"]
#     assert (cfg["start_date"], cfg["end_date"]) == ("", "")
#     monkeypatch.delenv("START_DATE", raising=False)
#     monkeypatch.delenv("END_DATE", raising=False)
#     assert resolve_date_range(cfg) == (None, None)
#     monkeypatch.setenv("START_DATE", "2026-03-01")
#     monkeypatch.setenv("END_DATE", "2026-04-30")
#     assert resolve_date_range(cfg) == (date(2026, 3, 1), date(2026, 4, 30))               # env beats config
#     assert resolve_date_range(cfg, "2026-02-01", None) == (date(2026, 2, 1), date(2026, 4, 30))   # CLI beats env
#     assert resolve_date_range({}) == (date(2026, 3, 1), date(2026, 4, 30))
#     monkeypatch.delenv("START_DATE")
#     monkeypatch.delenv("END_DATE")
#     assert resolve_date_range({}) == (None, None)


# def test_bad_dates_are_rejected(monkeypatch):
#     monkeypatch.delenv("START_DATE", raising=False)
#     monkeypatch.delenv("END_DATE", raising=False)
#     for bad in ("01-01-2026", "2026-13-01", "yesterday"):
#         with pytest.raises(ValueError):
#             parse_iso_date(bad)
#     with pytest.raises(ValueError):
#         resolve_date_range({"start_date": "2026-10-22", "end_date": "2026-10-21"})
#     assert main.main(["--start-date", "not-a-date"]) == 1                                # exits before any browser starts
#     assert main.main(["--start-date", "2026-10-22", "--end-date", "2026-10-21"]) == 1


# def test_without_dates_nothing_is_filtered():
#     res, data = scrape_product(FakePage(total=12), "P", URL, {**DEFAULT_SETTINGS}, SEL)
#     assert res["valid_reviews"] == 12 and res["coverage"] == "not_date_filtered"
#     assert res["reviews_before_range"] == 0


# # =========================================================================== end to end
# def test_process_product_end_to_end_with_date_range(monkeypatch):
#     today = date.today()
#     # reviews: 0..4 recent (in range), 5.. much older (month-year a year back)
#     older = f"{calendar.month_abbr[today.month]} {today.day:02d}, {today.year - 2}"
#     date_fn = lambda i: f"{i + 1} days ago" if i < 5 else older                           # noqa: E731
#     with tempfile.TemporaryDirectory() as d:
#         dirs = {"out": Path(d), "diag": Path(d)}
#         monkeypatch.setattr(fk_browser, "LivePage", lambda drv, st, diag: FakePage(total=40, date_fn=date_fn))
#         settings = {**DEFAULT_SETTINGS, "keyword_feedback_enabled": False, "max_reviews": 1000, "max_scroll_rounds": 200,
#                     "start_date": (today - timedelta(days=30)).isoformat(), "end_date": today.isoformat()}
#         res = main.process_product(None, URL, "Boat: Range", settings, SEL, dirs)
#         assert res["status"] == "success", res
#         assert res["reviews_in_range"] == 5 == res["valid_reviews"] and res["coverage"] == "complete_to_start_date"
#         assert res["positive_reviews"] + res["negative_reviews"] == 5
#         assert res["date_start"] == settings["start_date"] and res["date_end"] == settings["end_date"]
#         wb = load_workbook(Path(d) / "Boat Range.xlsx")
#         rows = [dict(zip(REVIEW_HEADERS, r)) for n in ("Positive Reviews", "Negative Reviews")
#                 for r in wb[n].iter_rows(min_row=5, max_col=15, values_only=True) if r[0]]
#         assert len(rows) == 5 and all(r["Scraped At"] not in (None, "N/A") for r in rows)
#         assert all(r["Date"] != r["Scraped At"] for r in rows)
#         assert wb["Star Ratings"]["B5"].value == 18587                                      # aggregates untouched, no padding
#         assert any("date window" in n for n in res["notes"])


# def test_real_fixture_parses_date_fields():
#     d = parse_reviews_detailed(fixture("reviews_airdopes800.html"), SEL, today=TODAY)
#     assert len(d["reviews"]) == 10
#     for r in d["reviews"]:
#         assert r["Date Precision"] in ("day", "week", "month", "year") and r["Date Latest"] and r["Date Status"]
