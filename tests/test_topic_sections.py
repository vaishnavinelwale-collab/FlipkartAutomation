# v6
"""Offline tests for topic-section scraping (no browser, no network).

Fixture HTML mimics Flipkart's two card layouts seen on the reviews page:
  * Overall list  : stars | title | text | name | , city | Verified Purchase | · N days ago
  * Topic list    : excerpt | "Name, City" | Certified Buyer | · N days ago   (no stars)
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fk_collect import ALL_SORTS, collect_all_sections, get_window       # noqa: E402
from fk_parsing import infer_sentiment, parse_reviews_detailed          # noqa: E402

TODAY = date(2026, 11, 10)
SELECTORS = {"review_block": ["div.NOPE"]}          # force the structure-based tiers


def d(t):
    return f"<div>{t}</div>"


def overall_card(stars, title, text, name, city, when):
    return "<div>" + "".join(d(x) for x in (
        f"{stars}.0", "•", title, text, name, f", {city}", "Helpful", "Verified Purchase", f"· {when}")) + "</div>"


def topic_card(excerpt, name_city, when, split_city=False, one_leaf_date=True):
    parts = [d(excerpt)]
    if split_city:
        n, c = name_city.rsplit(", ", 1)
        parts += [d(n), d(f", {c}")]
    else:
        parts.append(d(name_city))
    parts.append(d(f"Certified Buyer· {when}") if one_leaf_date else d("Certified Buyer") + d(f"· {when}"))
    return "<div>" + "".join(parts) + "</div>"


def page_html(cards):
    return "<html><body><div>" + "".join(cards) + "</div></body></html>"


# --------------------------------------------------------------------------- parsing
def test_topic_cards_are_parsed_without_a_star_rating():
    html = page_html([
        topic_card("ANC NOT GOOD BLACK GROUND SOUND TOO MUCH COMMING ,NOT GOOD FOR CALLING, BAD  PRODUCT",
                   "Robin Sebastian, Alappuzha", "15 days ago"),
        topic_card("Great sound quality,comfortablefit,andthe batterylifeeasilylastsallday....",
                   "Jahid Khan, Morena", "6 months ago", split_city=True, one_leaf_date=False),
        topic_card("...Not satisfied, Totally waste of money.", "Tanuja Yamana, Chipurupalle", "15 days ago"),
    ])
    out = parse_reviews_detailed(html, SELECTORS, TODAY)
    assert out["tier"] == "tier3_topic"
    assert len(out["reviews"]) == 3
    r = out["reviews"][0]
    assert r["User Name"] == "Robin Sebastian" and r["Location"] == "Alappuzha"
    assert r["Ratings"] == "N/A" and r["Topic Card"] is True
    assert r["Date"] == "Oct 26, 2026" and r["Verified Purchase"] == "Yes"
    assert out["reviews"][1]["User Name"] == "Jahid Khan" and out["reviews"][1]["Location"] == "Morena"


def test_rated_overall_cards_still_use_the_old_parser():
    html = page_html([overall_card(5, "Superb", "Loved the sound", "Amit", "Pune", "2 days ago"),
                      overall_card(1, "Bad", "Stopped working", "Neha", "Delhi", "3 days ago"),
                      overall_card(4, "Good", "Nice bass", "Ravi", "Goa", "5 days ago")])
    out = parse_reviews_detailed(html, SELECTORS, TODAY)
    assert out["tier"] == "tier2" and [r["Ratings"] for r in out["reviews"]] == [5, 1, 4]


def test_infer_sentiment():
    assert infer_sentiment("This sound quality is so poor and I get a headache") == "Negative"
    assert infer_sentiment("Very Nice Earbuds And Very Nice Sound Quality") == "Positive"
    assert infer_sentiment("ANC NOT GOOD BLACK GROUND SOUND TOO MUCH COMMING") == "Negative"


# --------------------------------------------------------------------------- full loop
class FakePage:
    """Serves a different HTML list for every (topic, sort) pair, like the live page."""

    def __init__(self, lists, topics, empty=()):
        self.lists, self.topics, self.empty = lists, topics, set(empty)
        self.topic, self.sort = topics[0], "MOST_RECENT"
        self.visited = []
        self.last_page_navigation = {}

    def html(self):
        return page_html(self.lists.get((self.topic, self.sort), []))

    def list_topics(self):
        return list(self.topics)

    def select_topic(self, label):
        self.topic = label
        return True

    def select_sort_order(self, sort):
        self.sort = sort
        if (self.topic, sort) in self.empty:
            self.last_page_navigation = {"status": "loaded_empty"}
            return False
        self.visited.append((self.topic, sort))
        return True

    def click_latest(self):
        return self.select_sort_order("MOST_RECENT")

    def expand_more(self):
        return 0

    def scroll_down(self):
        pass

    def date_count(self):
        return len(parse_reviews_detailed(self.html(), SELECTORS, TODAY)["reviews"])

    def wait_for_change(self, prev, timeout):
        return False


SETTINGS = {"start_date": "2026-08-10", "end_date": "2026-11-10", "scroll_patience": 1,
            "max_scroll_rounds": 2, "date_stop_streak": 3, "include_uncertain_dates": True,
            "fast_mode": False}          # the full 4-sorts x every-topic plan; fast mode has its own test below


def build_page():
    amit = overall_card(5, "Superb sound", "Loved the sound quality and the battery lasts all day", "Amit", "Pune", "2 days ago")
    neha = overall_card(1, "Bad", "ANC not good background sound too much coming", "Neha", "Delhi", "3 days ago")
    old = overall_card(5, "Old", "Very old review about sound", "Oldman", "Agra", "2 years ago")
    lists = {}
    for s in ALL_SORTS:
        lists[("Overall", s)] = [amit, neha, old]
    sq_cards = [
        topic_card("Loved the sound quality", "Amit, Pune", "2 days ago"),               # = rated Amit
        topic_card("ANC not good background sound too much coming", "Neha, Delhi", "3 days ago"),  # = rated Neha
        topic_card("This sound quality is so poor and I get a headache", "Kunal Gupta, Gautam Buddha Nagar", "2 months ago"),
        topic_card("Very Nice Earbuds And Very Nice Sound Quality", "Ankit Saini, Jaipur", "21 days ago"),
        topic_card("Great sound quality from the earbuds", "Jahid Khan, Morena", "6 months ago"),   # before window
    ]
    bass_cards = [
        topic_card("The bass is boomy and strong", "Ankit Saini, Jaipur", "21 days ago"),   # same reviewer/date as above
        topic_card("No bass at all, waste of money", "Flipkart Customer, Chennai", "10 days ago"),
        topic_card("Bass is excellent for the price", "Flipkart Customer, Chennai", "10 days ago"),  # different person, generic name
    ]
    for s in ALL_SORTS:
        lists[("Sound Quality", s)] = sq_cards
        lists[("Bass", s)] = bass_cards
    return lists


def test_every_topic_and_sort_is_visited_and_merged():
    topics = ["Overall", "Sound Quality", "Bass"]
    page = FakePage(build_page(), topics)
    reviews, info = collect_all_sections(page, SELECTORS, dict(SETTINGS), None, get_window(SETTINGS), TODAY)

    # 3 topics x 4 sorts, the very first list (Overall/Latest) is read by collect_reviews itself
    planned = {(t, s) for t in topics for s in ALL_SORTS}
    assert set(page.visited) | {("Overall", "MOST_RECENT")} == planned
    assert all(x["status"] == "collected" for x in info["sections"]) and len(info["sections"]) == 12

    by_name = {(r["User Name"], r["Short Review"][:12]): r for r in reviews}
    names = [r["User Name"] for r in reviews]
    assert "Oldman" not in names and "Jahid Khan" not in names          # outside the date window
    # Amit / Neha: topic excerpts folded into the rated Overall cards -> star rating inherited, no duplicates
    assert names.count("Amit") == 1 and names.count("Neha") == 1
    amit = next(r for r in reviews if r["User Name"] == "Amit")
    assert amit["Ratings"] == 5
    assert "Sound Quality" in amit["Source Sections"] and "Overall" in amit["Source Sections"]
    # Ankit Saini appears in two topics with different excerpts -> ONE review
    assert names.count("Ankit Saini") == 1
    ankit = next(r for r in reviews if r["User Name"] == "Ankit Saini")
    assert "Bass" in ankit["Source Sections"] and "Sound Quality" in ankit["Source Sections"]
    # two different 'Flipkart Customer, Chennai' reviews stay separate
    assert names.count("Flipkart Customer") == 2
    # unrated ones carry an inferred sentiment, flagged as such
    kunal = next(r for r in reviews if r["User Name"] == "Kunal Gupta")
    assert kunal["Ratings"] == "N/A" and kunal["Inferred Sentiment"] == "Negative" and "Inferred" in kunal["Rating Source"]
    assert ankit["Inferred Sentiment"] == "Positive"
    assert info["coverage"] == "multi_section_unverified"
    assert info["topic_merge"]["matched_to_rated"] == 2


def test_empty_sort_tab_is_recorded_not_fatal():
    topics = ["Overall", "Sound Quality"]
    page = FakePage(build_page(), topics, empty=[("Sound Quality", "POSITIVE_FIRST")])
    reviews, info = collect_all_sections(page, SELECTORS, dict(SETTINGS), None, get_window(SETTINGS), TODAY)
    row = next(x for x in info["sections"] if x["topic"] == "Sound Quality" and x["sort"] == "POSITIVE_FIRST")
    assert row["status"] == "loaded_empty"
    assert any(r["User Name"] == "Kunal Gupta" for r in reviews)


def test_real_flipkart_nesting_title_is_a_different_branch_than_name_row():
    """Mirrors the saved live page: title in nested spans, name and ', City' in separate divs,
    'Certified Buyer' and ' · Jun, 2023' in their own divs, every card wrapped in several divs."""
    def real_card(title, name, city, when, desc=None):
        t = f"<div><span><span></span><span>{title}</span></span></div>"
        if desc:
            t += f"<div><span><span>{desc}</span></span></div>"
        row = f"<div><div><div>{name}</div><div>, {city}</div></div></div>" if city else f"<div><div>{name}</div></div>"
        foot = f"<div><div><img alt=''><div>Certified Buyer</div></div><div> · {when}</div></div>"
        return f"<div><div><div>{t}<div>{row}{foot}</div></div></div></div>"

    html = page_html([
        real_card("Nice product big bass", "Flipkart Customer", "Mahesana", "Jun, 2023"),
        real_card("Value For Money", "Viku Yadav", "Pratapgarh", "24 days ago", desc="Best Sound with Bass"),
        real_card("Good sound &amp;amp; bass as expected", "Mrinmay Kundu", "Birbhum", "17 days ago"),
    ])
    out = parse_reviews_detailed(html, SELECTORS, TODAY)
    assert out["detected"] == 3 and len(out["reviews"]) == 3
    a, b, c = out["reviews"]
    assert (a["Short Review"], a["User Name"], a["Location"], a["Date"]) == \
           ("Nice product big bass", "Flipkart Customer", "Mahesana", "Jun, 2023")
    assert a["Date Precision"] == "month"
    assert b["Short Review"] == "Value For Money" and b["Review Description"] == "Best Sound with Bass"
    assert c["Short Review"] == "Good sound & bass as expected" and c["Date"] == "Oct 24, 2026"


# --------------------------------------------------------------------------- time limits
import time as _time                                                     # noqa: E402

import fk_collect                                                        # noqa: E402


class GrowingPage(FakePage):
    """A topic list that yields one more review on every scroll and never ends."""

    def __init__(self, topics):
        super().__init__({}, topics)
        self.n = 3
        self.scrolls = 0

    def html(self):
        cards = [topic_card(f"Review {i} about sound", f"Person{i} Name, City", "5 days ago") for i in range(self.n)]
        return page_html(cards)

    def scroll_down(self):
        self.scrolls += 1
        self.n += 1

    def wait_for_change(self, prev, timeout):
        _time.sleep(0.02)
        return True


def test_never_ending_list_is_stopped_by_the_section_time_limit():
    s = dict(SETTINGS, max_scroll_rounds=100000, scroll_patience=5, max_section_seconds=0.5,
             multi_section_enabled=False, start_date="", end_date="")
    page = GrowingPage(["Overall"])
    t = _time.time()
    reviews, info = collect_all_sections(page, SELECTORS, s, None, (None, None), TODAY)
    assert _time.time() - t < 5
    assert info["stop_reason"] == "time_limit" and 0 < page.scrolls < 100000


def test_exhausted_run_budget_returns_partial_results_and_marks_skipped_lists():
    topics = ["Overall", "Sound Quality", "Bass"]
    page = FakePage(build_page(), topics)
    s = dict(SETTINGS, deadline_epoch=_time.time() - 1)            # budget already used up
    reviews, info = collect_all_sections(page, SELECTORS, s, None, get_window(SETTINGS), TODAY)
    assert reviews is not None and info["time_limited"] is True
    skipped = [x for x in info["sections"] if x["status"] == "time_budget_exhausted"]
    assert len(skipped) == 11 and page.visited == []                 # nothing but the first list was read


def test_stop_signal_ends_the_run_early():
    page = FakePage(build_page(), ["Overall", "Sound Quality"])
    fk_collect.STOP.set()
    try:
        reviews, info = collect_all_sections(page, SELECTORS, dict(SETTINGS), None, get_window(SETTINGS), TODAY)
    finally:
        fk_collect.STOP.clear()
    assert info["time_limited"] is True


def test_latest_lists_of_all_topics_are_read_before_other_sorts():
    topics = ["Overall", "Sound Quality", "Bass"]
    page = FakePage(build_page(), topics)
    collect_all_sections(page, SELECTORS, dict(SETTINGS), None, get_window(SETTINGS), TODAY)
    order = page.visited
    last_latest = max(i for i, (_, so) in enumerate(order) if so == "MOST_RECENT")
    first_other = min(i for i, (_, so) in enumerate(order) if so != "MOST_RECENT")
    assert last_latest < first_other


def test_nothing_is_skipped_with_default_settings():
    """No budget, no per-list limit: all 7 topics x 4 sorts are read and nothing is marked skipped."""
    topics = ["Overall", "Sound Quality", "Bass", "Design & Build", "Battery Backup", "Value for Money", "Call Quality"]
    page = FakePage(build_page(), topics)
    reviews, info = collect_all_sections(page, SELECTORS, dict(SETTINGS), None, get_window(SETTINGS), TODAY)
    assert len(info["sections"]) == 28
    assert all(x["status"] == "collected" for x in info["sections"])
    assert info["time_limited"] is False


def test_excel_keeps_month_dated_reviews_that_may_be_in_range(tmp_path):
    """'2 months ago' cannot be pinned to a day; it must stay in the workbook (labelled), not vanish."""
    from openpyxl import load_workbook
    import Flipkart_Reviews_Hearables as F          # noqa: F401  (needs no browser)
    from fk_excel import update_workbook
    topics = ["Overall", "Sound Quality", "Bass"]
    page = FakePage(build_page(), topics)
    revs, _ = collect_all_sections(page, SELECTORS, dict(SETTINGS), None, get_window(SETTINGS), TODAY)
    for r in revs:
        r["Scraped At"] = "now"
    neg, pos = F.build_negative_df(revs, {1, 2, 3}), F.build_positive_df(revs, {1, 2, 3})
    path = str(tmp_path / "r.xlsx")
    update_workbook(path, "P", "2026-11-10", {5: 1, 1: 1}, 3.0, [], neg, pos, {}, date_range=get_window(SETTINGS),
                    run_info={"scraped_at": "now", "coverage": "x", "stop_reason": "y"}, include_uncertain=True)
    wb = load_workbook(path)
    rows = [r for n in ("Positive Reviews", "Negative Reviews") for r in wb[n].iter_rows(min_row=5, values_only=True) if r[0]]
    names = {r[0] for r in rows}
    assert "Kunal Gupta" in names                    # '2 months ago' review kept
    kunal = next(r for r in rows if r[0] == "Kunal Gupta")
    # '2 months ago' = Aug 11..Sep 10, entirely inside this test's range -> kept and labelled as approximate
    assert "Approximate (month)" in str(kunal[12]) and "entirely inside" in str(kunal[12])


def _stub_selenium():
    import types
    from unittest.mock import MagicMock
    for name in ("selenium", "selenium.webdriver", "selenium.webdriver.common", "selenium.webdriver.common.by",
                 "selenium.webdriver.common.keys", "selenium.webdriver.support", "selenium.webdriver.support.ui",
                 "selenium.webdriver.support.expected_conditions", "selenium.common", "selenium.common.exceptions",
                 "selenium.webdriver.chrome", "selenium.webdriver.chrome.options", "selenium.webdriver.chrome.service",
                 "selenium.webdriver.remote", "selenium.webdriver.remote.webelement"):
        if name not in sys.modules:
            sys.modules[name] = MagicMock()
    class WDE(Exception):
        pass
    sys.modules["selenium.common.exceptions"].WebDriverException = WDE
    sys.modules["selenium.common.exceptions"].TimeoutException = WDE
    sys.modules["selenium.common.exceptions"].NoSuchElementException = WDE
    return WDE


def test_end_of_list_is_detected_in_seconds_not_the_full_batch_timeout():
    _stub_selenium()
    import fk_browser
    pg = object.__new__(fk_browser.LivePage)
    pg.settings = {"end_of_list_quiet_seconds": 1.0}
    pg._height = lambda: 5000                       # page height never changes: nothing is loading
    pg.date_count = lambda: 20
    pg.nudge_scroll = lambda: None
    t = _time.time()
    changed = pg.wait_for_change(20, 60)            # old code would have waited the full timeout
    assert changed is False and _time.time() - t < 4


def test_new_batch_is_returned_immediately():
    _stub_selenium()
    import fk_browser
    pg = object.__new__(fk_browser.LivePage)
    pg.settings = {"end_of_list_quiet_seconds": 6}
    heights = iter([5000, 5000, 7000, 7000])
    counts = iter([30, 30, 30])
    pg._height = lambda: next(heights, 7000)
    pg.date_count = lambda: next(counts, 30)
    pg.nudge_scroll = lambda: None
    t = _time.time()
    assert pg.wait_for_change(20, 60) is True and _time.time() - t < 5


def test_fast_mode_reads_latest_everywhere_and_other_sorts_only_for_overall():
    topics = ["Overall", "Sound Quality", "Bass", "Design & Build"]
    page = FakePage(build_page(), topics)
    reviews, info = collect_all_sections(page, SELECTORS, dict(SETTINGS, fast_mode=True), None,
                                         get_window(SETTINGS), TODAY)
    visited = set(page.visited) | {("Overall", "MOST_RECENT")}
    assert {(t, "MOST_RECENT") for t in topics} <= visited                       # every topic, newest first
    assert {("Overall", s) for s in ("NEGATIVE_FIRST", "MOST_HELPFUL", "POSITIVE_FIRST")} <= visited
    assert not any(t != "Overall" and s != "MOST_RECENT" for t, s in visited)    # no slow extra lists
    assert len(info["sections"]) == 4 + 3 and reviews


# --------------------------------------------------------------------------- unlimited scroll / recovery
class StallingPage(FakePage):
    """One list that reveals one more review per scroll. Optional stall / driver failure injection."""

    def __init__(self, total, stall_at=None, fail_at=None, loader_visible_on_stall=True):
        super().__init__({}, ["Overall"])
        self.total, self.shown, self.scrolls = total, 1, 0
        self.stall_at, self.fail_at, self.loader = stall_at, fail_at, loader_visible_on_stall
        self.stalled = False

    def cards(self):
        return [overall_card(1 + i % 5, f"T{i}", f"Unique review text number {i} about sound", f"User{i}", "Pune",
                             "2 days ago") for i in range(self.shown)]

    def html(self):
        return page_html(self.cards())

    def scroll_down(self):
        self.scrolls += 1
        if self.fail_at == self.scrolls:
            self.fail_at = None
            raise RuntimeError("renderer timed out")
        if self.stall_at == self.scrolls and not self.stalled:
            self.stalled = True                   # this batch is slow: nothing new appears this round
            return
        self.shown = min(self.total, self.shown + 1)

    def has_more(self):
        return self.loader and self.shown < self.total


def test_zero_max_scroll_rounds_means_no_fixed_limit():
    from fk_collect import collect_reviews
    page = StallingPage(80)
    s = {"scroll_patience": 1, "max_scroll_rounds": 0, "list_recoveries": 0}
    reviews, info = collect_reviews(page, SELECTORS, s, None, None, TODAY)
    assert len(reviews) == 80 and info["stop_reason"] == "no_more_reviews_loaded"     # ended by the list, not a cap


def test_nonzero_max_scroll_rounds_is_still_a_cap():
    from fk_collect import collect_reviews
    reviews, info = collect_reviews(StallingPage(80), SELECTORS,
                                    {"scroll_patience": 1, "max_scroll_rounds": 10}, None, None, TODAY)
    assert info["stop_reason"] == "max_scroll_rounds" and len(reviews) < 80


def test_stalled_batch_is_recovered_when_the_loader_is_still_visible():
    from fk_collect import collect_reviews
    page = StallingPage(30, stall_at=10)
    s = {"scroll_patience": 1, "max_scroll_rounds": 0, "list_recoveries": 2}
    reviews, info = collect_reviews(page, SELECTORS, s, None, None, TODAY)
    assert len(reviews) == 30 and info["recoveries"] == 1


def test_stall_without_loader_is_a_genuine_end():
    from fk_collect import collect_reviews
    page = StallingPage(30, stall_at=10, loader_visible_on_stall=False)
    reviews, info = collect_reviews(page, SELECTORS, {"scroll_patience": 1, "max_scroll_rounds": 0}, None, None, TODAY)
    assert len(reviews) < 30 and info["recoveries"] == 0


def test_interrupted_list_keeps_reviews_and_has_no_duplicates_after_retry():
    page = StallingPage(25, fail_at=7)
    s = dict(SETTINGS, scroll_patience=1, max_scroll_rounds=0, start_date="", end_date="", section_retries=2,
             list_recoveries=2)
    reviews, info = collect_all_sections(page, SELECTORS, s, None, (None, None), TODAY)
    names = [r["User Name"] for r in reviews]
    assert len(names) == 25 and len(set(names)) == 25


# --------------------------------------------------------------------------- parallel browsers
class CloneablePage(FakePage):
    def __init__(self, lists, topics, dead_clone=False, shared=None):
        super().__init__(lists, topics)
        self.dead_clone = dead_clone
        self.visited = shared if shared is not None else []
        self.is_clone = shared is not None
        self.closed = False

    def clone(self):
        return CloneablePage(self.lists, self.topics, shared=self.visited, dead_clone=self.dead_clone)

    def select_topic(self, label):
        if self.is_clone and self.dead_clone:
            raise RuntimeError("invalid session id")
        return super().select_topic(label)

    def close(self):
        self.closed = True


def _plan(topics, sorts=ALL_SORTS):
    return {(t, s) for t in topics for s in sorts}


def test_parallel_browsers_read_every_list_and_match_the_sequential_result():
    topics = ["Overall", "Sound Quality", "Bass", "Design & Build", "Battery Backup", "Value for Money", "Call Quality"]
    seq_page = CloneablePage(build_page(), topics)
    seq, seq_info = collect_all_sections(seq_page, SELECTORS, dict(SETTINGS), None, get_window(SETTINGS), TODAY)
    par_page = CloneablePage(build_page(), topics)
    par, par_info = collect_all_sections(par_page, SELECTORS, dict(SETTINGS, parallel_workers=4), None,
                                         get_window(SETTINGS), TODAY)
    assert set(par_page.visited) | {("Overall", "MOST_RECENT")} == _plan(topics)       # all 28 lists, none skipped
    assert len(par_info["sections"]) == 28 and all(x["status"] == "collected" for x in par_info["sections"])
    key = lambda rs: sorted((r["User Name"], r["Short Review"], str(r["Ratings"])) for r in rs)
    assert key(par) == key(seq)                                                       # same reviews, no duplicates
    assert len(par) == len({(r["User Name"], r["Short Review"], str(r["Ratings"])) for r in par})


def test_a_dead_helper_browser_hands_its_lists_back_and_nothing_is_lost():
    topics = ["Overall", "Sound Quality", "Bass"]
    page = CloneablePage(build_page(), topics, dead_clone=False)
    orig_clone = page.clone
    page.clone = lambda: (lambda c: (setattr(c, "dead_clone", True), c)[1])(orig_clone())
    reviews, info = collect_all_sections(page, SELECTORS, dict(SETTINGS, parallel_workers=3), None,
                                         get_window(SETTINGS), TODAY)
    assert all(x["status"] == "collected" for x in info["sections"]) and len(info["sections"]) == 12
    assert set(page.visited) | {("Overall", "MOST_RECENT")} == _plan(topics)


# --------------------------------------------------------------------------- date rules
def _state(label, start, end):
    from fk_parsing import parse_review_date, review_window_state
    rec = parse_review_date(label, date(2026, 11, 10))
    return review_window_state({"Date Earliest": rec["earliest"].isoformat(), "Date Latest": rec["latest"].isoformat()},
                               start, end)


def test_approximate_dates_are_checked_against_the_users_range():
    # user asks for the whole of 2026: 'August 2026' is certainly inside -> accepted; a 2025 month is outside
    y26 = (date(2026, 1, 1), date(2026, 12, 31))
    assert _state("Aug, 2026", *y26) == "in"
    assert _state("Dec, 2025", *y26) == "before"
    # user asks for the last 3 weeks (Oct 20 - Nov 10): '3 weeks ago' straddles the boundary -> rejected
    w3 = (date(2026, 10, 20), date(2026, 11, 10))
    assert _state("3 weeks ago", *w3) == "uncertain"
    assert _state("Aug, 2026", *w3) == "before"
    assert _state("2 days ago", *w3) == "in"
    # a 2-month request: '3 weeks ago' (Oct 14-20) is certainly inside
    assert _state("3 weeks ago", date(2026, 9, 10), date(2026, 11, 10)) == "in"


def test_uncertain_dates_are_rejected_but_fully_inside_approximate_dates_are_kept():
    from fk_collect import collect_reviews
    page = type("P", (), {})()
    cards = [overall_card(1, "a", "review about week label", "WeekGuy", "Pune", "3 weeks ago"),
             overall_card(2, "b", "review about August month", "AugGuy", "Pune", "Aug, 2026"),
             overall_card(3, "c", "recent review two days", "DayGuy", "Pune", "2 days ago")]
    class P(FakePage):
        def html(self):
            return page_html(cards)
    def run(start, end, **kw):
        revs, _ = collect_reviews(P({}, ["Overall"]), SELECTORS, dict(scroll_patience=1, max_scroll_rounds=1, **kw),
                                  None, (start, end), TODAY)
        return {r["User Name"] for r in revs}
    # whole of 2026: the August and week-label reviews all lie inside -> kept
    assert run(date(2026, 1, 1), date(2026, 12, 31), include_uncertain_dates=False) == {"WeekGuy", "AugGuy", "DayGuy"}
    # last 3 weeks: '3 weeks ago' overlaps the start boundary -> rejected; August is outside -> rejected
    assert run(date(2026, 10, 20), date(2026, 11, 10), include_uncertain_dates=False) == {"DayGuy"}
