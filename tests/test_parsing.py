"""Offline parsing tests. Fixtures in tests/fixtures are trimmed copies of REAL Flipkart pages
saved by the scraper on 2026-10-16.   Run:  python -m pytest tests -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import fixture, text_of  # noqa: E402
from fk_common import DEFAULT_SELECTORS  # noqa: E402
from fk_parsing import (build_review_url, classify_page, classify_review, is_sorted_recent,  # noqa: E402
                        is_valid_review, normalize_date, parse_keyword_percentages,
                        parse_rating_summary, parse_reviews, parse_reviews_detailed, review_key, with_page)

SEL = DEFAULT_SELECTORS


# ---- real page structure ---------------------------------------------------
def test_real_page_extracts_all_fields():
    d = parse_reviews_detailed(fixture("reviews_airdopes800.html"), SEL)
    assert d["tier"] == "tier2" and len(d["reviews"]) == 10 and d["detected"] == 10 and d["invalid"] == 0
    r = d["reviews"][0]
    assert r["Ratings"] == 5 and isinstance(r["Ratings"], int)
    assert r["Short Review"] == "Just wow!"
    assert r["Review Description"] == "Nice product & value for money"      # '&amp;' decoded
    assert r["User Name"] == "Ankit Choudhary"                              # not 'Helpful'
    assert r["Location"] == "Jamshedpur"
    assert r["Variant"] == "Color Interstellar Black • Microphone Yes"
    assert r["Verified Purchase"] == "Yes" and r["Helpful Votes"] == 0
    assert r["Date"] != "N/A"
    assert all(is_valid_review(x) for x in d["reviews"])


def test_real_pages_helpful_votes_and_truncation():
    revs, _ = parse_reviews(fixture("reviews_rockerz210.html"), SEL)
    assert any(r["Helpful Votes"] not in ("N/A", 0) for r in revs) or len(revs) == 10
    assert all(r["User Name"] not in ("Helpful", "Verified Purchase") for r in revs)
    assert all("&amp;" not in " ".join(map(str, r.values())) for r in revs)


def test_seven_review_page():
    d = parse_reviews_detailed(fixture("reviews_katana_7.html"), SEL)
    assert len(d["reviews"]) == 7


def test_real_page_sorted_newest_first():
    d = parse_reviews_detailed(fixture("reviews_airdopes800.html"), SEL)
    assert is_sorted_recent(d["reviews"]) is True
    assert is_sorted_recent(list(reversed(d["reviews"]))) is False


# ---- missing fields / entities / odd formats --------------------------------
def _block(*leaves):
    return "<div>" + "".join(f"<div>{x}</div>" for x in leaves) + "</div>"


def _page(*blocks):
    return "<html><body>" + "".join(blocks) + "</body></html>"


def test_missing_variant_city_and_helpful():
    html = _page(_block("4.0", "•", "Nice", "Good bass &amp; clear", "Priya", "Verified Purchase", "· 2 days ago"),
                 _block("5.0", "•", "Great", "Loved it", "Sam", "· 3 days ago"),
                 _block("3.0", "•", "Ok", "Fine", "Raj", "· 4 days ago"))
    revs, tier = parse_reviews(html, SEL)
    assert tier == "tier2" and len(revs) == 3
    assert revs[0]["Review Description"] == "Good bass & clear"
    assert revs[0]["Variant"] == "N/A" and revs[0]["Location"] == "N/A"
    assert revs[0]["Helpful Votes"] == "N/A"
    assert revs[0]["User Name"] == "Priya"


def test_rating_formats():
    html = _page(_block("5", "•", "A", "x text", "N1", "· 1 day ago"),
                 _block("4.0", "•", "B", "y text", "N2", "· 2 days ago"),
                 _block("1.0", "•", "C", "z text", "N3", "· 3 days ago"))
    assert [r["Ratings"] for r in parse_reviews(html, SEL)[0]] == [5, 4, 1]


def test_title_only_review_is_valid_and_helpful_for_n():
    html = _page(_block("2.0", "•", "Bad", "Review for: Color Red", "Anil", ", Pune", "Helpful for 3", "7",
                        "Verified Purchase", "· 5 days ago"),
                 _block("1.0", "•", "Awful", "Review for: Color Red", "Bob", ", Goa", "Helpful", "· 6 days ago"),
                 _block("5.0", "•", "Fine", "Review for: Color Red", "Cy", ", Agra", "Helpful", "· 7 days ago"))
    revs, _ = parse_reviews(html, SEL)
    assert revs[0]["Helpful Votes"] == 7 and revs[0]["Short Review"] == "Bad"
    assert revs[0]["Review Description"] == "N/A" and is_valid_review(revs[0])


def test_truncated_flag_and_longer_text_wins_dedupe():
    cut = _block("1.0", "•", "Bad", "Review for: X", "It broke after a week of use...", "more", "Z", "Helpful", "· 1 day ago")
    revs, _ = parse_reviews(_page(cut, cut.replace("1 day", "2 days"), cut.replace("1 day", "3 days")), SEL)
    assert revs[0]["Truncated"] is True
    assert review_key(revs[0]) == review_key(revs[1])


def test_empty_and_garbage_pages():
    assert parse_reviews("<html><body></body></html>", SEL) == ([], "none")
    assert parse_reviews("<html><body><p>Access denied</p></body></html>", SEL) == ([], "none")
    assert parse_reviews("", SEL) == ([], "none")


def test_tier2_after_all_classes_renamed():
    html = ('<section><article><span>5<i></i></span><h3>Great sound</h3><span>Love the bass.</span><b>READ MORE</b>'
            '<em>Ravi</em><em>Certified Buyer, Pune</em><time>3 months ago</time><u>12</u></article>'
            '<article><span>1<i></i></span><h3>Not connecting</h3><span>App login otp problem</span>'
            '<em>Asha</em><em>Certified Buyer, Delhi</em><time>Jan, 2025</time><u>3</u></article>'
            '<article><span>4<i></i></span><h3>Good</h3><span>Works</span><em>Li</em>'
            '<em>Certified Buyer, Agra</em><time>5 days ago</time></article></section>')
    revs, tier = parse_reviews(html, SEL)
    assert tier == "tier2" and [r["Ratings"] for r in revs] == [5, 1, 4]
    assert revs[0]["User Name"] == "Ravi" and revs[0]["Review Description"] == "Love the bass."


# ---- histogram ----------------------------------------------------------------
def test_histogram_real_page_ascending_and_verified():
    s = parse_rating_summary(text_of(fixture("reviews_airdopes800.html")))
    assert s["counts"] == {1: 3002, 2: 1264, 3: 2511, 4: 6271, 5: 18587}
    assert s["total_ratings"] == 31635 and s["total_reviews"] == 2726
    assert s["verified"] is True and s["average"] == 4.14


def test_histogram_descending_layout_also_works():
    t = "10 ratings and 2 reviews\n5\n★\n4\n4\n★\n3\n3\n★\n1\n2\n★\n1\n1\n★\n1"
    s = parse_rating_summary(t)
    assert s["counts"][5] == 4 and s["verified"]


def test_histogram_rejects_counts_that_do_not_match_total():
    t = "999 ratings and 5 reviews\n1\n★\n3\n2\n★\n1\n3\n★\n2\n4\n★\n6\n5\n★\n18"
    s = parse_rating_summary(t)
    assert s["counts"] == {} and s["verified"] is False and s["found_unverified"][5] == 18


def test_histogram_absent():
    s = parse_rating_summary("nothing to see here")
    assert s["counts"] == {} and s["average"] is None


# ---- page state -----------------------------------------------------------------
def test_classify_page_states():
    assert classify_page("u", "t", "Please complete the CAPTCHA", 0)[0] == "blocked"
    assert classify_page("https://www.flipkart.com/account/login?x", "Login", "", 0)[0] == "login_required"
    assert classify_page("u", "Page Not Found", "404", 0)[0] == "not_found"
    assert classify_page("u", "X Reviews", "31,635 ratings and 2,726 reviews", 0)[0] == "empty"
    assert classify_page("u", "t", "hello", 0)[0] == "unknown"
    assert classify_page("u", "t", "Verified Purchase", 5)[0] == "ok"   # never flags a good page


# ---- URL / dates / classification ------------------------------------------------
def test_review_url_builder():
    u = ("https://www.flipkart.com/boat-airdopes-800/p/itm58ac9c83ac391?pid=ACCHY8GFFEYX75RH"
         "&lid=LSTX&marketplace=FLIPKART&q=boat&srno=s_1_1")
    r = build_review_url(u)
    assert "/boat-airdopes-800/product-reviews/itm58ac9c83ac391" in r
    assert "pid=ACCHY8GFFEYX75RH" in r and "sortOrder=MOST_RECENT" in r and "srno" not in r
    assert "page=3" in with_page(r, 3)
    alternate = with_page(r, 2, "positive_first")
    assert "page=2" in alternate and "sortOrder=POSITIVE_FIRST" in alternate
    live_sort = with_page("https://www.flipkart.com/x/product-reviews/itm?pid=A&aid=overall&certifiedBuyer=false&pageUID=123",
                          2, "MOST_HELPFUL")
    assert all(token in live_sort for token in ("aid=overall", "certifiedBuyer=false", "pageUID=123",
                                                 "page=2", "sortOrder=MOST_HELPFUL"))
    assert build_review_url("https://example.com/nothing") is None


def test_classifier_fixes():
    assert classify_review("my blog about technology")[1] == "Generic Comments"
    assert classify_review("this is not good")[1] != "Positive Feedback"
    assert classify_review("otp login fails")[1] == "Login"
    assert classify_review("battery drains")[1] == "Battery"
    assert classify_review("")[1] == "Generic Comments"


def test_dates_and_keywords():
    assert normalize_date("· 2 days ago") != "N/A"
    assert normalize_date("Jan, 2025") == "Jan, 2025"
    assert normalize_date("") == "N/A"
    assert parse_keyword_percentages("Battery\n82% positive\n18% negative") == ("82%", "18%")
