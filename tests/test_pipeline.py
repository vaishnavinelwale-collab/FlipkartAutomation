"""Pipeline tests: pagination, statuses, Excel output. Offline (FakePage + real HTML fixtures)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import load_workbook  # noqa: E402

import fk_collect  # noqa: E402
from fakes import FakePage, fixture, page_html  # noqa: E402
from fk_collect import scrape_product  # noqa: E402
from fk_common import DEFAULT_SELECTORS, DEFAULT_SETTINGS  # noqa: E402
from fk_excel import safe_filename, update_workbook  # noqa: E402
from Flipkart_Reviews_Hearables import build_negative_df, classify_sentiment, load_products  # noqa: E402

SEL = DEFAULT_SELECTORS
SET = {**DEFAULT_SETTINGS, "max_reviews": 200}
URL = "https://www.flipkart.com/x/p/itm1?pid=A"


def run(page, **over):
    return scrape_product(page, "Prod", URL, {**SET, **over}, SEL)


# ---- statuses -------------------------------------------------------------------
def test_success_with_infinite_scroll():
    page = FakePage(total=35)
    res, data = run(page)
    assert res["status"] == "success", res
    assert res["valid_reviews"] == 35 == len(data["reviews"])
    assert res["pagination_attempted"] and res["pagination_mode"] == "infinite_scroll" and res["scroll_rounds"] >= 3
    assert res["histogram_verified"] and data["summary"]["counts"][5] == 18587
    assert res["review_elements_detected"] >= 10 and res["final_url"].endswith("pid=A")
    assert res["page_title"] and res["failure_reason"] == ""
    assert len({r["User Name"] for r in data["reviews"]}) == 35   # no duplicates


def test_max_reviews_cap():
    res, data = run(FakePage(total=100), max_reviews=25)
    assert res["valid_reviews"] == 25 and res["status"] == "success"


def test_stalled_scroll_does_not_use_unreliable_page_number_fallback():
    page = FakePage(total=25, mode="pages", date_fn=lambda i: "Sep 10, 2026")
    res, data = run(page, start_date="2026-09-01", end_date="2026-09-22", scroll_patience=2)
    assert res["pagination_mode"] == "infinite_scroll"
    assert res["valid_reviews"] == 10
    assert res["stop_reason"] == "no_more_reviews_loaded"
    assert res["coverage"] == "stopped_unverified" and res["status"] == "partial"


def test_truncated_text_expanded_and_counted():
    page = FakePage(total=10, truncated_until_expand=range(10))
    res, data = run(page)
    assert res["truncated_reviews"] == 0 and res["status"] == "success"
    assert all("..." not in r["Review Description"] for r in data["reviews"])


def test_truncated_not_expandable_is_partial():
    class Stuck(FakePage):
        def expand_more(self):
            return 0
    res, _ = run(Stuck(total=10, truncated_until_expand=range(10)))
    assert res["status"] == "partial" and res["truncated_reviews"] == 10


def test_partial_when_histogram_missing_reviews_still_extracted():
    res, data = run(FakePage(total=12, with_histogram=False))
    assert res["status"] == "partial" and res["valid_reviews"] == 12
    assert res["histogram_extracted"] is False and "histogram" in res["failure_reason"]


def test_real_fixture_is_success_and_reports_pagination_attempt():
    res, data = run(FakePage(static_html=fixture("reviews_airdopes800.html")))
    assert res["status"] == "success" and res["valid_reviews"] == 10
    assert res["histogram_verified"] and res["total_reviews_on_page"] == 2726
    assert res["pagination_attempted"] is True
    assert any("collected 10 of 2726" in n for n in res["notes"])


def test_no_reviews_page():
    html = "<html><head><title>X Reviews</title></head><body>31,635 ratings and 2,726 reviews</body></html>"
    page = FakePage(total=0, static_html=html, title="X Reviews")
    res, data = run(page)
    assert res["status"] == "no_reviews" and data["reviews"] == [] and page.snapshots
    assert "no review" in res["failure_reason"].lower()


def test_blocked_captcha_page():
    html = "<html><head><title>Verify</title></head><body>Please complete the CAPTCHA to continue</body></html>"
    page = FakePage(reached=False, static_html=html, title="Verify", final_url="https://www.flipkart.com/captcha")
    res, _ = run(page)
    assert res["status"] == "blocked" and "captcha" in res["failure_reason"].lower() and page.snapshots


def test_login_wall_reported_as_blocked_not_success():
    page = FakePage(reached=False, static_html="<html><body>Login</body></html>",
                    final_url="https://www.flipkart.com/account/login?ret=x")
    res, _ = run(page)
    assert res["status"] == "blocked" and "login" in res["failure_reason"].lower()


def test_navigation_failure():
    page = FakePage(reached=False, static_html="<html><body>hello</body></html>",
                    nav_reason="direct reviews URL: no review entries appeared within 25s")
    res, _ = run(page)
    assert res["status"] == "navigation_failed" and "25s" in res["failure_reason"]


def test_unexpected_error_is_reported_not_raised():
    res, data = run(FakePage(total=10, raise_on_html=True))
    assert res["status"] == "error" and "browser crashed" in res["failure_reason"] and data["reviews"] == []


def test_histogram_failure_does_not_invalidate_reviews():
    res, data = run(FakePage(total=10, with_histogram=False))
    assert res["valid_reviews"] == 10 and not res["histogram_extracted"]


def test_page_shown_but_zero_parseable_is_not_success():
    html = "<html><head><title>R Reviews</title></head><body><div>5.0</div><div>no date here</div></body></html>"
    res, _ = run(FakePage(total=0, static_html=html, title="R Reviews"))
    assert res["status"] != "success"


# ---- Excel -----------------------------------------------------------------------
REVIEWS = [
    {"User Name": "A", "Ratings": 1, "Short Review": "bad", "Review Description": "otp login fails",
     "Date": "Jan 05, 2025", "Variant": "Color Red", "Location": "Pune", "Helpful Votes": 2, "Verified Purchase": "Yes"},
    {"User Name": "B", "Ratings": 5, "Short Review": "great", "Review Description": "good", "Date": "Jan 05, 2025"},
    {"User Name": "C", "Ratings": "N/A", "Short Review": "x", "Review Description": "y", "Date": "Jan 05, 2025"},
    {"User Name": "D", "Ratings": 3, "Short Review": "meh", "Review Description": "battery drains", "Date": "Jan 05, 2025"},
]


def test_negative_positive_classification():
    neg = build_negative_df(REVIEWS, {1, 2, 3})
    assert [r["User Name"] for r in neg] == ["A", "D"]
    assert neg[0]["Consolidated Reason"] == "Login" and neg[1]["Consolidated Reason"] == "Battery"
    assert classify_sentiment(5, {1, 2, 3}) == "Positive" and classify_sentiment(3, {1, 2, 3}) == "Negative"


def test_excel_values_columns_and_rerun_dedupe():
    neg = build_negative_df(REVIEWS, {1, 2, 3})
    c1, c2 = {5: 100, 4: 50, 3: 10, 2: 5, 1: 5}, {5: 110, 4: 55, 3: 12, 2: 5, 1: 6}
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        assert update_workbook(p, "Prod", "2026-01-01", c1, 4.1, [], neg) == ([], 2)
        neg_drift = [{**r, "Date": "Jan 09, 2025"} for r in neg]           # relative dates drift between runs
        assert update_workbook(p, "Prod", "2026-01-02", c2, 4.2, [], neg_drift) == ([], 0)
        wb = load_workbook(p)
        s = wb["Star Ratings"]
        assert s["B10"].value == 150 and s["B11"].value == 20 and s["C5"].value == 110 and s["C16"].value == 10
        assert s.max_column == 3
        assert "Reviews" not in wb.sheetnames
        rv = wb["Negative Reviews"]
        hdr = [c.value for c in rv[4]][:12]
        assert hdr[-4:] == ["Variant", "Location", "Helpful Votes", "Verified Purchase"]
        row = [c.value for c in rv[5]]
        assert row[0] == "A" and row[1] == 1 and row[8:12] == ["Color Red", "Pune", 2, "Yes"]
        rows = [r for r in rv.iter_rows(min_row=5, max_col=12, values_only=True) if r[0]]
        assert [r[0] for r in rows] == ["A", "D"]    # only genuine reviews; rerun did not duplicate them
        # aggregate counts live on Star Ratings only; they never create rows in the review sheets
        assert [s["C5"].value, s["C7"].value, s["C8"].value, s["C9"].value] == [110, 12, 5, 6]
        pos = [r[0] for r in wb["Positive Reviews"].iter_rows(min_row=5, max_col=12, values_only=True) if r[0]]
        assert pos == []                             # this fixture has no positive reviews passed in
        assert "Positive Negative Values" not in wb.sheetnames   # no keyword data -> no empty sheet


def test_unknown_histogram_is_NA_never_zero():
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        update_workbook(p, "Prod", "2026-01-01", {}, None, [], [])
        s = load_workbook(p)["Star Ratings"]
        assert [s.cell(r, 2).value for r in range(5, 13)] == ["N/A"] * 8
        assert s["B16"].value == "-"


def test_missing_classification_columns_do_not_crash():
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        problems, added = update_workbook(p, "Prod", "2026-01-01", {}, None, [],
                                          [{"User Name": "A", "Ratings": 1}])
        assert problems == [] and added == 1
        assert load_workbook(p)["Negative Reviews"]["F5"].value == "Unclassified"


def test_exactly_four_required_sheets_even_when_keyword_data_exists():
    kw = [{"Keywords": "Sound", "Positive Reviews": "80%", "Negative Reviews": "20%"}]
    kw2 = [{"Keywords": "Sound", "Positive Reviews": "85%", "Negative Reviews": "15%"},
           {"Keywords": "Battery", "Positive Reviews": "70%", "Negative Reviews": "30%"}]
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "t.xlsx")
        update_workbook(p, "P", "2026-01-01", {}, None, kw, [])
        assert update_workbook(p, "P", "2026-01-02", {}, None, kw2, [])[0] == []
        assert load_workbook(p).sheetnames == ["Star Ratings", "Positive Reviews", "Negative Reviews", "Time"]


def test_corrupt_workbook_is_replaced_not_fatal():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        p.write_bytes(b"not a zip")
        assert update_workbook(str(p), "P", "2026-01-01", {}, None, [], [])[0] == []
        assert list(Path(d).glob("*.corrupt-*"))


def test_filenames_and_csv_loader():
    assert safe_filename('Rockerz 255: Z+/Pro?') == "Rockerz 255 Z+Pro.xlsx"
    assert safe_filename("") == "product.xlsx"
    p = Path(tempfile.mkdtemp()) / "u.csv"
    p.write_text("\ufeffURLs,Product Name\nhttps://www.flipkart.com/x-y/p/itm1?pid=A,Boat X\nnot-a-url,Bad\n", encoding="utf-8")
    assert load_products(p) == [("https://www.flipkart.com/x-y/p/itm1?pid=A", "Boat X")]


# ---- end to end through process_product (fake page, real Excel) -----------------------
def test_process_product_end_to_end(monkeypatch=None):
    import fk_browser
    import Flipkart_Reviews_Hearables as main
    with tempfile.TemporaryDirectory() as d:
        dirs = {"out": Path(d), "diag": Path(d)}
        orig = fk_browser.LivePage
        fk_browser.LivePage = lambda drv, st, diag: FakePage(total=23)
        try:
            settings = {**SET, "keyword_feedback_enabled": False}
            res = main.process_product(None, URL, "Boat: Test", settings, SEL, dirs)
        finally:
            fk_browser.LivePage = orig
        assert res["status"] == "success" and res["valid_reviews"] == 23
        assert res["positive_reviews"] + res["negative_reviews"] == 23
        wb = load_workbook(Path(d) / "Boat Test.xlsx")
        sr = wb["Star Ratings"]
        counts = {5: sr["B5"].value, 4: sr["B6"].value, 3: sr["B7"].value, 2: sr["B8"].value, 1: sr["B9"].value}
        assert counts[5] == 18587
        def per_star(name):
            vals = [r[1] for r in wb[name].iter_rows(min_row=5, max_col=12, values_only=True) if r[0]]
            return {s: vals.count(s) for s in set(vals)}
        # only individually extracted reviews are written; aggregate counts do not add rows
        assert sum(per_star("Positive Reviews").values()) == res["positive_reviews"]
        assert sum(per_star("Negative Reviews").values()) == res["negative_reviews"]
        assert res["positive_reviews"] + res["negative_reviews"] == 23 < counts[5]
        names = [r[0] for n in ("Positive Reviews", "Negative Reviews")
                 for r in wb[n].iter_rows(min_row=5, max_col=12, values_only=True) if r[0]]
        assert "Rating only (no review)" not in names
        assert res["negative_rows_written"] == res["negative_reviews"] > 0
        assert res["positive_rows_written"] == res["positive_reviews"] > 0


def test_process_product_failure_writes_no_workbook():
    import fk_browser
    import Flipkart_Reviews_Hearables as main
    with tempfile.TemporaryDirectory() as d:
        dirs = {"out": Path(d), "diag": Path(d)}
        orig = fk_browser.LivePage
        fk_browser.LivePage = lambda drv, st, diag: FakePage(
            reached=False, static_html="<html><body>CAPTCHA</body></html>")
        try:
            res = main.process_product(None, URL, "Blocked Prod", {**SET, "keyword_feedback_enabled": False}, SEL, dirs)
        finally:
            fk_browser.LivePage = orig
        assert res["status"] == "blocked" and not list(Path(d).glob("*.xlsx"))


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
