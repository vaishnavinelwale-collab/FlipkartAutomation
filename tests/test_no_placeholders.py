"""
Regression tests: aggregate star-rating totals must NEVER create customer records.
Offline only (FakePage + saved HTML fixtures + temporary workbooks).
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from openpyxl import Workbook, load_workbook  # noqa: E402

import fk_excel  # noqa: E402
from fakes import FakePage, fixture  # noqa: E402
from fk_collect import scrape_product  # noqa: E402
from fk_common import DEFAULT_SELECTORS, DEFAULT_SETTINGS  # noqa: E402
from fk_excel import REVIEW_HEADERS, update_workbook  # noqa: E402
from Flipkart_Reviews_Hearables import build_negative_df, build_positive_df  # noqa: E402

PLACEHOLDER = "Rating only (no review)"
HUGE = {5: 18587, 4: 6271, 3: 2511, 2: 1264, 1: 3002}      # aggregate counts >> extracted reviews
URL = "https://www.flipkart.com/x/p/itm1?pid=A"


def _rv(name, rating, text, date="Jan 05, 2025", **extra):
    return {"User Name": name, "Ratings": rating, "Short Review": f"title {name}", "Review Description": text,
            "Date": date, **extra}


def _data_rows(wb, sheet):
    return [r for r in wb[sheet].iter_rows(min_row=5, max_col=len(REVIEW_HEADERS), values_only=True) if r[0]]


REVIEWS = [
    _rv("Asha", 5, "love the sound", Location="Pune", **{"Helpful Votes": 3, "Verified Purchase": "Yes"}),
    _rv("Ravi", 4, "good battery"),
    _rv("Meena", 3, "average, battery drains"),
    _rv("Kiran", 1, "otp login fails"),
]


def _build(path, counts=HUGE, reviews=REVIEWS, run_date="2026-10-21"):
    neg, pos = build_negative_df(reviews, {1, 2, 3}), build_positive_df(reviews, {1, 2, 3})
    return update_workbook(str(path), "Prod", run_date, counts, 4.2, [], neg, pos)


# ---- the core guarantee -----------------------------------------------------------
def test_aggregate_counts_do_not_create_placeholder_records():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        assert _build(p)[0] == []
        wb = load_workbook(p)
        pos, neg = _data_rows(wb, "Positive Reviews"), _data_rows(wb, "Negative Reviews")
        assert [r[0] for r in pos] == ["Asha", "Ravi"]               # 2 real, not 24,858
        assert [r[0] for r in neg] == ["Meena", "Kiran"]             # 2 real, not 6,777
        for r in pos + neg:
            assert r[0] != PLACEHOLDER and r[5] != "No review text"
        # a second run (e.g. counts grew) still adds nothing
        _build(p, counts={5: 20000, 4: 7000, 3: 3000, 2: 1500, 1: 3500}, run_date="2026-10-22")
        wb = load_workbook(p)
        assert len(_data_rows(wb, "Positive Reviews")) == 2 and len(_data_rows(wb, "Negative Reviews")) == 2


def test_no_reviews_extracted_means_empty_review_sheets_not_filled_ones():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p, reviews=[])
        wb = load_workbook(p)
        assert _data_rows(wb, "Positive Reviews") == [] and _data_rows(wb, "Negative Reviews") == []
        assert wb["Star Ratings"]["B5"].value == 18587              # aggregates still reported


def test_placeholder_builder_is_gone():
    assert not hasattr(fk_excel, "_rating_only_row")
    assert not hasattr(fk_excel, "RATING_ONLY_NAME") and not hasattr(fk_excel, "RATING_ONLY_NOTE")


def test_real_fixture_end_to_end_row_count_equals_extracted_reviews():
    sel, st = DEFAULT_SELECTORS, {**DEFAULT_SETTINGS, "max_reviews": 200}
    res, data = scrape_product(FakePage(static_html=fixture("reviews_airdopes800.html")), "P", URL, st, sel)
    assert res["histogram_verified"] and sum(data["summary"]["counts"].values()) == 31635
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        neg, pos = build_negative_df(data["reviews"], {1, 2, 3}), build_positive_df(data["reviews"], {1, 2, 3})
        update_workbook(str(p), "P", "2026-10-21", data["summary"]["counts"], data["summary"]["average"], [], neg, pos)
        wb = load_workbook(p)
        total = len(_data_rows(wb, "Positive Reviews")) + len(_data_rows(wb, "Negative Reviews"))
        assert total == len(data["reviews"]) == 10 < 31635


# ---- Star Ratings keeps the aggregates ---------------------------------------------
def test_star_ratings_sheet_still_has_aggregates_and_overall():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p)
        s = load_workbook(p)["Star Ratings"]
        assert [s.cell(r, 1).value for r in range(5, 10)] == [f"Rating{i}" for i in (5, 4, 3, 2, 1)]
        assert [s.cell(r, 2).value for r in range(5, 10)] == [18587, 6271, 2511, 1264, 3002]
        assert s["B10"].value == 18587 + 6271 and s["B11"].value == 2511 + 1264 + 3002
        assert s["B12"].value == 4.2


def test_comparison_table_is_labelled_as_aggregate_vs_extracted_and_adds_no_rows():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p)
        ws = load_workbook(p)["Negative Reviews"]
        assert ws["S4"].value.startswith("Aggregate count, all dates")
        assert ws["T4"].value.startswith("Individually extracted reviews")
        assert [ws.cell(r, 19).value for r in (5, 6, 7)] == [2511, 1264, 3002]     # 3,2,1 star aggregates
        text = " ".join(str(c.value) for row in ws.iter_rows(min_col=18, max_col=18) for c in row if c.value)
        assert "only reviews that were individually extracted" in text


# ---- field handling: never invent ---------------------------------------------------
def test_missing_fields_stay_NA_and_nothing_is_invented():
    bare = {"User Name": "Solo", "Ratings": 2, "Short Review": "bad", "Review Description": "bad sound"}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p, reviews=[bare])
        row = _data_rows(load_workbook(p), "Negative Reviews")[0]
        h = dict(zip(REVIEW_HEADERS, row))
        assert h["Date"] == "N/A" and h["Location"] == "N/A"
        assert h["Helpful Votes"] == "N/A" and h["Verified Purchase"] == "N/A" and h["Variant"] == "N/A"
        assert h["User Name"] == "Solo" and h["Review Description"] == "bad sound"


def test_available_fields_are_kept():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p)
        h = dict(zip(REVIEW_HEADERS, _data_rows(load_workbook(p), "Positive Reviews")[0]))
        assert (h["User Name"], h["Ratings"], h["Review Description"], h["Date"]) == ("Asha", 5, "love the sound", "Jan 05, 2025")
        assert (h["Location"], h["Helpful Votes"], h["Verified Purchase"]) == ("Pune", 3, "Yes")


def test_run_timestamp_is_separate_from_review_date():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p, run_date="2026-10-21")
        wb = load_workbook(p)
        for sheet in ("Positive Reviews", "Negative Reviews"):
            for r in _data_rows(wb, sheet):
                h = dict(zip(REVIEW_HEADERS, r))
                assert h["Run Date"] == "2026-10-21"            # when the scraper ran
                assert h["Date"] == "Jan 05, 2025"              # when the customer wrote the review
        assert wb["Time"].cell(2, 1).value                       # generation time stays on its own sheet
        assert all(r[0] != wb["Time"].cell(2, 1).value for r in _data_rows(wb, "Positive Reviews"))
    # a review with no date must not borrow the run date
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p, reviews=[{"User Name": "NoDate", "Ratings": 5, "Short Review": "x", "Review Description": "y"}],
               run_date="2026-10-21")
        h = dict(zip(REVIEW_HEADERS, _data_rows(load_workbook(p), "Positive Reviews")[0]))
        assert h["Date"] == "N/A" and h["Run Date"] == "2026-10-21"


# ---- negative filtering + formatting still work -------------------------------------
def test_negative_filter_rules_unchanged():
    reviews = [_rv(str(i), i, f"text {i}") for i in (1, 2, 3, 4, 5)] + [_rv("x", "N/A", "unrated")]
    assert [r["Ratings"] for r in build_negative_df(reviews, {1, 2, 3})] == [1, 2, 3]
    assert [r["Ratings"] for r in build_positive_df(reviews, {1, 2, 3})] == [4, 5]
    assert [r["Ratings"] for r in build_negative_df(reviews, {1, 2})] == [1, 2]       # config-driven
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p, reviews=reviews[:5])
        wb = load_workbook(p)
        assert sorted(r[1] for r in _data_rows(wb, "Negative Reviews")) == [1, 2, 3]
        assert sorted(r[1] for r in _data_rows(wb, "Positive Reviews")) == [4, 5]


def test_excel_formatting_preserved():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        _build(p)
        wb = load_workbook(p)
        assert wb.sheetnames == ["Star Ratings", "Positive Reviews", "Negative Reviews", "Time"]
        for name in ("Positive Reviews", "Negative Reviews"):
            ws = wb[name]
            assert ws["A1"].value == "Product Name: Prod" and ws["A1"].font.bold and ws["A1"].fill.start_color.rgb.endswith("8DBF51")
            assert "A1:B1" in [str(r) for r in ws.merged_cells.ranges]
            assert [c.value for c in ws[4]][:len(REVIEW_HEADERS)] == REVIEW_HEADERS and ws[4][0].font.bold
            assert ws.freeze_panes == "A5" and ws.column_dimensions["D"].width == 60 and ws.column_dimensions["B"].width == 10


# ---- legacy placeholder rows are removed, genuine rows survive -----------------------
def _legacy_workbook(path, n_placeholders=50):
    """A workbook shaped like the ones the old version wrote: real rows + padding rows."""
    pad = [PLACEHOLDER, 5, "", "", "N/A", "No review text", "No review text", "2026-10-20", "N/A", "N/A", "N/A", "N/A"]
    _build(path, reviews=[])
    wb = load_workbook(path)
    pos, neg = wb["Positive Reviews"], wb["Negative Reviews"]
    real_pos = ["Asha", 5, "title Asha", "love the sound", "Jan 05, 2025", "Positive Feedback", "Positive Feedback",
                "2026-10-20", "N/A", "Pune", 3, "Yes"]
    real_neg = ["Kiran", 1, "title Kiran", "otp login fails", "Jan 05, 2025", "Login", "Login",
                "2026-10-20", "N/A", "N/A", "N/A", "N/A"]
    pos.append(real_pos), neg.append(real_neg)
    for _ in range(n_placeholders):
        pos.append(pad), neg.append(list(pad[:1]) + [1] + pad[2:])
    wb.save(path)
    return real_pos, real_neg


def test_legacy_placeholder_rows_removed_on_next_update_but_real_reviews_kept():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.xlsx"
        real_pos, real_neg = _legacy_workbook(p)
        stats = {}
        neg, pos = build_negative_df([], {1, 2, 3}), build_positive_df([], {1, 2, 3})
        update_workbook(str(p), "Prod", "2026-10-21", HUGE, 4.2, [], neg, pos, stats)
        wb = load_workbook(p)
        assert [r[:12] for r in _data_rows(wb, "Positive Reviews")] == [tuple(real_pos)]
        assert [r[:12] for r in _data_rows(wb, "Negative Reviews")] == [tuple(real_neg)]
        assert stats["pos_placeholders_removed"] == 50 and stats["neg_placeholders_removed"] == 50


def test_cleanup_tool_backs_up_first_then_cleans_only_placeholder_rows():
    import clean_placeholder_rows as tool
    with tempfile.TemporaryDirectory() as d:
        reports = Path(d) / "reports"
        reports.mkdir()
        dirty, clean = reports / "dirty.xlsx", reports / "clean.xlsx"
        real_pos, real_neg = _legacy_workbook(dirty)
        _build(clean)
        clean_bytes = clean.read_bytes()
        dirty_bytes = dirty.read_bytes()
        tool.BASE_DIR = Path(d)                                   # keep the backup inside the temp dir
        assert tool.main(["--dir", str(reports), "--dry-run"]) == 0
        assert dirty.read_bytes() == dirty_bytes and not (Path(d) / "backups").exists()   # dry run: no change
        assert tool.main(["--dir", str(reports)]) == 0
        backups = list((Path(d) / "backups").glob("reports_*"))
        assert len(backups) == 1 and (backups[0] / "dirty.xlsx").read_bytes() == dirty_bytes   # original preserved
        assert clean.read_bytes() == clean_bytes                  # workbook without placeholders untouched
        wb = load_workbook(dirty)
        assert [r[:12] for r in _data_rows(wb, "Positive Reviews")] == [tuple(real_pos)]
        assert [r[:12] for r in _data_rows(wb, "Negative Reviews")] == [tuple(real_neg)]
        assert wb["Star Ratings"]["B5"].value == 18587            # aggregates intact


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
