# v6
"""
Pure parsing logic (no Selenium) so it can be unit-tested offline.

Resilience strategy
-------------------
TIER 1  known CSS selectors from fk_config.json  (fast, exact)
TIER 2  layout-independent heuristics that rely on what a review *is*
        (a rating digit, a title, a body, a date) rather than on class names.
Flipkart can rename every CSS class and TIER 2 will still work.
"""
import calendar
import html as _html
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

import importlib.util

# lxml is optional: it parses a big Flipkart page several times faster when it is installed, and the
# scraper silently uses Python's built-in "html.parser" when it is not (requirements.txt need not change).
_PARSER = "lxml" if importlib.util.find_spec("lxml") else "html.parser"

# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #

def build_review_url(product_url):
    """
    Turn a product URL into its reviews URL, e.g.
      /slug/p/itmXXXX?pid=..  ->  /slug/product-reviews/itmXXXX?pid=..&sortOrder=MOST_RECENT
    This avoids clicking an 'All reviews' button whose class names change.
    Returns None if the URL shape is not recognised.
    """
    p = urlparse(str(product_url).strip())
    parts = [s for s in p.path.split("/") if s]
    if "product-reviews" in parts:
        new_path = p.path
    elif "p" in parts and parts.index("p") + 1 < len(parts):
        i = parts.index("p")
        slug = "/".join(parts[:i])
        new_path = f"/{slug}/product-reviews/{parts[i + 1]}"
    else:
        return None
    q = parse_qs(p.query)
    keep = {k: q[k][0] for k in ("pid", "lid", "marketplace") if k in q}
    keep.setdefault("marketplace", "FLIPKART")
    keep["sortOrder"] = "MOST_RECENT"
    return urlunparse((p.scheme or "https", p.netloc or "www.flipkart.com", new_path, "", urlencode(keep), ""))


def with_page(url, page, sort_order=None):
    p = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    q["page"] = str(page)
    if sort_order:
        q["sortOrder"] = str(sort_order).upper()
    return urlunparse((p.scheme, p.netloc, p.path, "", urlencode(q), ""))


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
_ABS_DATE_TEXT = (
    r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s*\d{4}|"
    r"\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|"
    r"[A-Za-z]{3,9},?\s+\d{4}"
)
_DATE_SEARCH = re.compile(
    rf"\b(\d+\s+(?:day|week|month|year)s?\s+ago|today|yesterday)\b|\b({_ABS_DATE_TEXT})\b",
    re.I,
)
_DATE_LEAF = re.compile(
    rf"^[\W_]*(\d+\s+(?:day|week|month|year)s?\s+ago|today|yesterday|{_ABS_DATE_TEXT})[\W_]*$",
    re.I,
)


def normalize_date(raw, now=None):
    """Return an absolute 'Mon DD, YYYY' for relative dates; keep absolute ones as found."""
    now = now or datetime.today()
    m = _DATE_SEARCH.search(raw or "")
    if not m:
        return (raw or "").strip() or "N/A"
    txt = (m.group(1) or m.group(2)).strip()
    low = txt.lower()
    if low == "today":
        return now.strftime("%b %d, %Y")
    if low == "yesterday":
        return (now - timedelta(days=1)).strftime("%b %d, %Y")
    rel = re.match(r"(\d+)\s+(day|week|month|year)", low)
    if rel:
        n, unit = int(rel.group(1)), rel.group(2)
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
        return (now - timedelta(days=days)).strftime("%b %d, %Y")
    return txt


# --------------------------------------------------------------------------- #
# Review-date model (used for the START_DATE..END_DATE filter)
#
# Flipkart shows either relative dates ('2 days ago', '3 months ago') or month-year dates
# ('Jan, 2026'). Only the first kind of the form 'today / yesterday / N days ago' pins down a day.
# Everything else is a RANGE of possible days; we keep that range (earliest..latest) instead of
# inventing a single day, and flag the record as uncertain.
# --------------------------------------------------------------------------- #
_REL = re.compile(r"\b(\d+|an?)\s+(minute|hour|day|week|month|year)s?\s+ago\b", re.I)
_MONTH_YEAR = re.compile(r"^([A-Za-z]{3,9})\.?,?\s+(\d{4})$")
_FULL_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y",
                      "%d %b, %Y", "%Y-%m-%d", "%d/%m/%Y")
_MONTHS = {**{m.lower(): i for i, m in enumerate(calendar.month_abbr) if m},
           **{m.lower(): i for i, m in enumerate(calendar.month_name) if m}}


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else None


def _add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def parse_iso_date(value):
    """'2026-01-01' -> date. Raises ValueError with a clear message."""
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"invalid date {value!r}: use YYYY-MM-DD (e.g. 2026-01-01)") from None


def parse_review_date(raw, today=None):
    """Interpret the date text Flipkart displays.

    Returns {'display', 'earliest', 'latest', 'precision', 'exact', 'source'}.
      * exact day known  -> earliest == latest, precision 'day'
      * only a period    -> earliest..latest span it, precision 'week' / 'month' / 'year'
      * not understood   -> earliest/latest None, precision 'none'
    Relative dates are resolved against `today` (the scraping date), never stored as the review date
    unless they identify a single day."""
    today = _as_date(today) or date.today()
    core = re.sub(r"^[\W_]+|[\W_]+$", "", re.sub(r"\s+", " ", str(raw or "").replace("\xa0", " "))).strip()
    core = re.sub(r",\s*", ", ", core)
    out = {"display": core or NA, "earliest": None, "latest": None, "precision": "none",
           "exact": False, "source": "none"}
    if not core or core.upper() == NA:
        out["display"] = NA
        return out

    def day(d, source):
        out.update(display=d.strftime("%b %d, %Y"), earliest=d, latest=d, precision="day", exact=True, source=source)
        return out

    def span(lo, hi, precision, source):
        out.update(earliest=lo, latest=hi, precision=precision, exact=False, source=source)
        return out

    low = core.lower()
    if low == "today":
        return day(today, "relative")
    if low == "yesterday":
        return day(today - timedelta(days=1), "relative")
    for fmt in _FULL_DATE_FORMATS:
        try:
            return day(datetime.strptime(core, fmt).date(), "absolute")
        except ValueError:
            continue
    m = _REL.fullmatch(core) or _REL.search(core)
    if m:
        n = 1 if m.group(1).lower() in ("a", "an") else int(m.group(1))
        unit = m.group(2).lower()
        if unit in ("minute", "hour"):
            return day(today, "relative")
        if unit == "day":
            return day(today - timedelta(days=n), "relative")
        if unit == "week":      # 'N weeks ago' = N*7 .. N*7+6 days ago
            return span(today - timedelta(days=7 * n + 6), today - timedelta(days=7 * n), "week", "relative")
        months = n if unit == "month" else 12 * n
        return span(_add_months(today, -(months + 1)) + timedelta(days=1), _add_months(today, -months),
                    unit, "relative")
    m = _MONTH_YEAR.match(core)
    if m and m.group(1).lower() in _MONTHS:
        mo, yr = _MONTHS[m.group(1).lower()], int(m.group(2))
        return span(date(yr, mo, 1), date(yr, mo, calendar.monthrange(yr, mo)[1]), "month", "month_year")
    return out


def window_state(earliest, latest, start, end):
    """Where does the possible-date range [earliest, latest] sit relative to the inclusive window
    [start, end] (either bound may be None = open)?  Returns one of
      'in'         every possible day is inside the window
      'before'     every possible day is earlier than `start`
      'after'      every possible day is later than `end`
      'uncertain'  the range overlaps a window boundary: it may or may not be inside
      'undated'    no usable date"""
    earliest, latest = _as_date(earliest), _as_date(latest)
    if earliest is None or latest is None:
        return "undated"
    if start and latest < start:
        return "before"
    if end and earliest > end:
        return "after"
    if (start is None or earliest >= start) and (end is None or latest <= end):
        return "in"
    return "uncertain"


def _iso_to_date(v):
    try:
        return date.fromisoformat(str(v)) if v not in (None, "", NA) else None
    except ValueError:
        return None


def review_window_state(r, start, end):
    """window_state() for a parsed review record (uses its stored Date Earliest / Date Latest)."""
    return window_state(_iso_to_date(r.get("Date Earliest")), _iso_to_date(r.get("Date Latest")), start, end)


def date_status_text(precision, state=None):
    """Human-readable 'Date Status' cell."""
    if precision == "day":
        return "Exact"
    if precision == "none":
        return "Unknown date"
    if state == "in":
        return f"Approximate ({precision}) - entirely inside the requested range"
    base = f"Uncertain - only the {precision} is known"
    return base + (" - overlaps range boundary" if state == "uncertain" else "")


# --------------------------------------------------------------------------- #
# Review parsing
#
# Verified against saved Flipkart pages (react-native-web layout, Oct 2026).
# One review is a block whose visible text leaves read, in order:
#   5.0 | • | <title> | Review for: <variant> | <text> [| more] | <name> | , <city>
#   | Helpful [for N] [| N] | Verified Purchase | · 2 days ago
# The class names (css-xxxx / r-xxxx) are generated and meaningless, so the parser
# works on this text structure instead of on classes.
# --------------------------------------------------------------------------- #
_RATING_LEAF = re.compile(r"^\s*([1-5])(?:\.0)?\s*★?\s*$")
_VARIANT = re.compile(r"^\s*review for\b:?\s*(.*)$", re.I)
_HELPFUL = re.compile(r"^\s*helpful(?:\s+for\s+(\d+))?\s*$", re.I)
_VERIFIED = re.compile(r"^\s*(verified purchase|certified buyer.*)\s*$", re.I)
_MORE = re.compile(r"^\s*(more|read more|less|show less)\s*$", re.I)
_CITY = re.compile(r"^\s*,\s*(.+)$")
_SEPARATOR = re.compile(r"^[•·|]+$")
_SKIP_TAGS = {"script", "style", "noscript", "svg", "path"}
NA = "N/A"


def _own_text(el):
    return re.sub(r"\s+", " ", "".join(el.find_all(string=True, recursive=False))).strip()


def _leaves(node):
    out = []
    for el in node.find_all(True):
        if el.name in _SKIP_TAGS:
            continue
        t = _own_text(el)
        if t:
            out.append(t)
    return out


def _rating_int(text):
    m = re.search(r"[1-5]", text or "")
    return int(m.group()) if m else None


def _first(el, selectors):
    for s in selectors or []:
        try:
            found = el.select_one(s)
        except Exception:
            continue
        if found is not None:
            return found
    return None


def _clean(text):
    """Decode HTML entities (the page double-escapes '&'), drop UI words, collapse whitespace."""
    text = _html.unescape(_html.unescape(text or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).replace("READ MORE", "").strip()


def _record(user, rating, short, desc, date_raw, variant="", location="", helpful=None,
            verified=None, truncated=False, today=None):
    d = parse_review_date(date_raw, today)
    return {
        "User Name": _clean(user) or NA,
        "Ratings": rating if rating is not None else NA,
        "Short Review": _clean(short) or NA,
        "Review Description": _clean(desc) or NA,
        # Review date as Flipkart shows it: an absolute day only when the day is really known,
        # otherwise the original wording ('3 months ago', 'Jan, 2026'). Never a guessed day.
        "Date": d["display"],
        "Date Earliest": d["earliest"].isoformat() if d["earliest"] else "",
        "Date Latest": d["latest"].isoformat() if d["latest"] else "",
        "Date Precision": d["precision"],
        "Date Status": date_status_text(d["precision"]),
        "Variant": _clean(variant) or NA,
        "Location": _clean(location) or NA,
        "Helpful Votes": helpful if helpful is not None else NA,
        "Verified Purchase": ("Yes" if verified else "No") if verified is not None else NA,
        "Truncated": bool(truncated),
    }


def is_valid_review(r):
    """A record counts only if it has a rating, a date and real text.

    Topic-section cards (Sound Quality, Bass, ...) show no star rating, so a record flagged
    'Topic Card' is valid without one; its rating is resolved later by fk_collect."""
    rated = isinstance(r.get("Ratings"), int) and 1 <= r["Ratings"] <= 5
    return ((rated or bool(r.get("Topic Card")))
            and r.get("Date") not in (None, "", NA)
            and (r.get("Review Description") != NA or r.get("Short Review") != NA))


def _tier1(soup, sel, today=None):
    """Configured CSS selectors (fk_config.json). Returns (records, detected_blocks)."""
    blocks = []
    for bs in sel.get("review_block", []):
        try:
            blocks = soup.select(bs)
        except Exception:
            blocks = []
        if blocks:
            break
    out = []
    for b in blocks:
        r, s, d = (_first(b, sel.get(k)) for k in ("rating", "short_review", "description"))
        u, dt = _first(b, sel.get("username")), _first(b, sel.get("date"))
        out.append(_record(
            u.get_text(" ") if u else "",
            _rating_int(r.get_text()) if r else None,
            s.get_text(" ") if s else "",
            d.get_text(" ") if d else "",
            dt.get_text(" ") if dt else "",
            today=today,
        ))
    return out, len(blocks)


def _rating_idx(leaves):
    for i, t in enumerate(leaves[:3]):
        if _RATING_LEAF.match(t):
            return i
    return None


def _fields_from_leaves(leaves, ri, di, today=None):
    """Turn the ordered text leaves of one review block into a record."""
    seq = leaves[ri + 1:di]
    variant = city = None
    helpful = None
    verified = False
    more_seen = False
    helpful_idx = city_idx = verified_idx = None
    txt = []  # (index, text) of free-text leaves: title, body, username
    prev_was_helpful = False
    for i, t in enumerate(seq):
        is_helpful_label = False
        if _SEPARATOR.match(t):
            pass
        elif _VARIANT.match(t) and variant is None:
            variant = _VARIANT.match(t).group(1)
        elif _MORE.match(t):
            more_seen = True
        elif _HELPFUL.match(t):
            m = _HELPFUL.match(t)
            helpful = int(m.group(1)) if m.group(1) else 0
            helpful_idx, is_helpful_label = i, True
        elif prev_was_helpful and re.fullmatch(r"\d{1,6}", t):
            helpful = int(t)
        elif _VERIFIED.match(t):
            verified, verified_idx = True, i
        elif _CITY.match(t) and city is None:
            city, city_idx = _CITY.match(t).group(1).strip(), i
        else:
            txt.append((i, t))
        prev_was_helpful = is_helpful_label

    marker = next((m for m in (city_idx, helpful_idx, verified_idx) if m is not None), None)
    username, body = NA, txt
    if marker is not None:
        before = [x for x in txt if x[0] < marker]
        if len(before) >= 2:                       # title + (body...) + name
            username = before[-1][1]
            body = [x for x in txt if x is not before[-1]]
    elif len(txt) >= 3 and len(txt[-1][1]) <= 40:  # unknown layout: last short line = name
        username, body = txt[-1][1], txt[:-1]
    title = body[0][1] if body else ""
    desc = " ".join(t for _, t in body[1:])
    truncated = more_seen or bool(re.search(r"(\.\.\.|…)\s*$", desc))
    return _record(username, _rating_int(leaves[ri]), title, desc, leaves[di], variant or "",
                   city or "", helpful, verified, truncated and bool(more_seen), today=today)


def _tier2(soup, today=None):
    """Structure-based: find date leaves, climb to the smallest ancestor that is one review.
    Returns (records, detected_blocks)."""
    anchors = [el for el in soup.find_all(True)
               if el.name not in _SKIP_TAGS and _DATE_LEAF.match(_own_text(el) or "")]
    seen, blocks = set(), []
    for a in anchors:
        node = a.parent
        while node is not None and getattr(node, "name", None) not in (None, "[document]", "html"):
            leaves = _leaves(node)
            n_dates = sum(1 for t in leaves if _DATE_LEAF.match(t))
            if n_dates > 1:
                break
            if n_dates == 1 and _rating_idx(leaves) is not None:
                if id(node) not in seen:
                    seen.add(id(node))
                    blocks.append(leaves)
                break
            node = node.parent
    out = []
    for leaves in blocks:
        ri = _rating_idx(leaves)
        di = next(i for i, t in enumerate(leaves) if _DATE_LEAF.match(t))
        if di > ri:
            out.append(_fields_from_leaves(leaves, ri, di, today))
    return out, len(blocks)


# --------------------------------------------------------------------------- #
# TIER 3: topic-section cards.
# Inside a topic chip (Sound Quality, Bass, Design & Build, ...) Flipkart shows a compact card with
# NO star rating:   <review excerpt> | <name> | , <city> | Certified Buyer | · 15 days ago
# (name and city may also arrive as one leaf "Robin Sebastian, Alappuzha").
# --------------------------------------------------------------------------- #
_TOPIC_DATE_LEAF = re.compile(
    rf"^[\W_]*(?:(?:certified buyer|verified purchase)[\W_]*)?"
    rf"(\d+\s+(?:day|week|month|year)s?\s+ago|today|yesterday|{_ABS_DATE_TEXT})[\W_]*$",
    re.I,
)
_ONLY_VERIFIED = re.compile(r"^[\W_]*(certified buyer|verified purchase)[\W_]*$", re.I)
_NEG_WORDS = re.compile(
    r"\b(poor|bad|worst|waste|wastage|useless|worthless|pathetic|terrible|horrible|disappoint\w*|"
    r"cheap|defect\w*|faulty|problem\w*|issues?|complain\w*|not\s+(?:good|satisfied|working|worth|"
    r"happy|great|nice|clear|comfortable|recommended?)|never\s+buy|don'?t\s+buy|do\s+not\s+buy|"
    r"noise|noisy|headache|breaking|garr+|crackl\w*|lag\w*|stopped|dead|low\s+quality|"
    r"very\s+low|too\s+low|no\s+bass|flat)\b",
    re.I,
)


def infer_sentiment(text):
    """'Negative' / 'Positive' from review wording. Used ONLY for topic cards whose star rating could
    not be recovered from another list; the record is flagged so nobody mistakes it for a rating."""
    return "Negative" if _NEG_WORDS.search(text or "") else "Positive"


def _topic_block_fields(leaves, today=None):
    di = next((i for i, t in enumerate(leaves) if _TOPIC_DATE_LEAF.match(t)), None)
    if di is None:
        return None
    date_raw = _TOPIC_DATE_LEAF.match(leaves[di]).group(1)
    rating = None
    txt, city, verified = [], None, False
    for t in leaves[:di]:
        if _SEPARATOR.match(t) or _MORE.match(t):
            continue
        if _VERIFIED.match(t) or _ONLY_VERIFIED.match(t):
            verified = True
        elif _RATING_LEAF.match(t) and not txt and rating is None:
            rating = _rating_int(t)
        elif _HELPFUL.match(t):
            continue
        elif _CITY.match(t) and txt and city is None:
            city = _CITY.match(t).group(1).strip()
        else:
            txt.append(t)
    if re.search(r"certified buyer|verified purchase", leaves[di], re.I):
        verified = True
    if len(txt) < 2:
        return None                           # need at least an excerpt and a reviewer line
    name = txt[-1]
    if city is None and "," in name and len(name) <= 70:   # "Name, City" in one leaf
        name, _, city = name.rpartition(",")
        name, city = name.strip(), city.strip()
    body = txt[:-1]
    title = body[0] if body else ""
    desc = " ".join(body[1:])
    rec = _record(name, rating, title, desc, date_raw, "", city or "", None, verified,
                  bool(re.search(r"(\.\.\.|…)\s*$", desc or title)), today)
    rec["Topic Card"] = True
    return rec


def _tier3(soup, today=None):
    """Topic cards. Returns (records, detected_blocks)."""
    anchors = [el for el in soup.find_all(True)
               if el.name not in _SKIP_TAGS and _TOPIC_DATE_LEAF.match(_own_text(el) or "")]
    seen, blocks = set(), []
    for a in anchors:
        node = a.parent
        while node is not None and getattr(node, "name", None) not in (None, "[document]", "html"):
            leaves = _leaves(node)
            n_dates = sum(1 for t in leaves if _TOPIC_DATE_LEAF.match(t))
            if n_dates > 1:
                break
            # a ", City" leaf is not card content on its own; without this the climb stops at the
            # name+city row and the review text (a sibling further up) is left out of the block
            others = [t for t in leaves if not _TOPIC_DATE_LEAF.match(t) and not _ONLY_VERIFIED.match(t)
                      and not _SEPARATOR.match(t) and not _CITY.match(t)]
            if n_dates == 1 and len(others) >= 2:
                if id(node) not in seen:
                    seen.add(id(node))
                    blocks.append(leaves)
                break
            node = node.parent
    out = []
    for leaves in blocks:
        rec = _topic_block_fields(leaves, today)
        if rec:
            out.append(rec)
    return out, len(blocks)


def _healthy(reviews):
    """Trust a parse only if most records are valid."""
    return bool(reviews) and sum(map(is_valid_review, reviews)) / len(reviews) >= 0.7


def parse_reviews_detailed(html, selectors, today=None):
    """{'reviews': valid records, 'detected': blocks seen, 'invalid': dropped, 'tier': ...}"""
    soup = BeautifulSoup(html, _PARSER)
    for tier, fn in (("tier1", lambda: _tier1(soup, selectors, today)), ("tier2", lambda: _tier2(soup, today)),
                     ("tier3_topic", lambda: _tier3(soup, today))):
        recs, detected = fn()
        if _healthy(recs):
            good = [r for r in recs if is_valid_review(r)]
            return {"reviews": good, "detected": detected, "invalid": len(recs) - len(good), "tier": tier}
    return {"reviews": [], "detected": 0, "invalid": 0, "tier": "none"}


def parse_reviews(html, selectors):
    """Backward-compatible: (reviews, tier)."""
    d = parse_reviews_detailed(html, selectors)
    return d["reviews"], d["tier"]


def review_key(r):
    """Identity of a review. Deliberately excludes the date: relative dates ('3 months ago')
    drift between runs and would create duplicates."""
    return (r["User Name"], r["Short Review"], r["Review Description"][:60])


_ABS_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%b, %Y", "%B, %Y", "%b %Y", "%B %Y")


def _to_date(s):
    for fmt in _ABS_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _sort_key(r):
    """Latest possible day of the review (works for relative and month-year dates too)."""
    return _iso_to_date(r.get("Date Latest")) or (_to_date(r.get("Date")).date() if _to_date(r.get("Date")) else None)


def is_sorted_recent(reviews, tolerance=0.9):
    """True if dates are (mostly) newest-first. Unknown if fewer than 3 dated reviews."""
    ds = [d for d in (_sort_key(r) for r in reviews) if d]
    if len(ds) < 3:
        return None
    ok = sum(1 for a, b in zip(ds, ds[1:]) if a >= b)
    return ok / (len(ds) - 1) >= tolerance


# --------------------------------------------------------------------------- #
# Rating summary (histogram), from page TEXT. Verified against the page's own total.
# Live layout lists the stars ASCENDING (1..5): "1 ★ 3,002 2 ★ 1,264 ... 5 ★ 18,587".
# --------------------------------------------------------------------------- #
def _tokens(text):
    text = (text or "").replace("★", "\n")
    return [t.strip() for t in text.splitlines() if t.strip()]


def _to_int(s):
    try:
        return int(str(s).replace(",", ""))
    except ValueError:
        return None


def parse_rating_summary(text):
    """
    Returns {'counts': {1..5: n} | {}, 'total_ratings', 'total_reviews', 'verified', 'average'}.
    'counts' is only filled when the five numbers add up to the page's own '<N> ratings' total,
    so a mis-read number can never be written as data.
    """
    tok = _tokens(text)
    out = {"counts": {}, "total_ratings": None, "total_reviews": None, "verified": False,
           "average": None, "found_unverified": {}}
    start = 0
    for i, t in enumerate(tok):
        m = re.search(r"([\d,]+)\s+ratings?\s+and\s+([\d,]+)\s+reviews?", t, re.I)
        if m:
            out["total_ratings"], out["total_reviews"] = _to_int(m.group(1)), _to_int(m.group(2))
            start = i
            break
        m = re.fullmatch(r"([\d,]+)\s+ratings?", t, re.I)
        if m:
            out["total_ratings"], start = _to_int(m.group(1)), i
            break
    for i in range(start, len(tok) - 9):
        stars = [tok[i + 2 * k] for k in range(5)]
        if stars not in (["1", "2", "3", "4", "5"], ["5", "4", "3", "2", "1"]):
            continue
        counts = [_to_int(tok[i + 2 * k + 1]) for k in range(5)]
        if any(c is None for c in counts):
            continue
        found = dict(zip(map(int, stars), counts))
        out["found_unverified"] = found
        if out["total_ratings"] is None or sum(counts) == out["total_ratings"]:
            out["counts"] = found
            out["verified"] = out["total_ratings"] is not None
            total = sum(counts)
            if total:
                out["average"] = round(sum(s * c for s, c in found.items()) / total, 2)
        break
    return out


def parse_star_counts(text):
    """Compatibility wrapper: (counts, None)."""
    return parse_rating_summary(text)["counts"], None


def parse_keyword_percentages(text):
    """Best-effort: look for '<n>% ... positive' / '<n>% ... negative' in visible text."""
    def grab(word):
        for pat in (rf"(\d{{1,3}})\s*%[^\n%]{{0,25}}?{word}", rf"{word}[^\n%]{{0,25}}?(\d{{1,3}})\s*%"):
            m = re.search(pat, text or "", re.I)
            if m:
                return f"{m.group(1)}%"
        return None
    return grab("positive"), grab("negative")


# --------------------------------------------------------------------------- #
# What kind of page is this? (used only when no reviews were found)
# --------------------------------------------------------------------------- #
_RESTRICTION_PHRASES = (
    "captcha", "i'm not a robot", "i am not a robot", "verify you are human", "unusual traffic",
    "access denied", "request blocked", "too many requests", "temporarily blocked",
    "automated access", "are you a robot",
)
_NOT_FOUND_PHRASES = ("page not found", "this page doesn't exist", "404", "no longer available",
                      "currently unavailable", "something went wrong")


def classify_page(url, title, text, reviews_detected=0):
    """Returns (state, reason). state in: ok | blocked | login_required | not_found | empty | unknown."""
    low = f"{title or ''}\n{text or ''}".lower()
    if reviews_detected > 0:
        return "ok", ""
    for p in _RESTRICTION_PHRASES:
        if p in low:
            return "blocked", f"access restriction / verification page detected ('{p}')"
    if "/account/login" in (url or "") or re.search(r"login\s*(?:/|&)?\s*signup|enter email/mobile", low):
        return "login_required", "Flipkart redirected to a login screen"
    for p in _NOT_FOUND_PHRASES:
        if p in low and "ratings and" not in low:
            return "not_found", f"error / missing page ('{p}')"
    if "ratings and" in low or "reviews" in (title or "").lower():
        return "empty", "reviews page loaded but contains no review entries"
    return "unknown", "page loaded but is not recognisable as a reviews page"


# --------------------------------------------------------------------------- #
# Negative-review classification (bug-fixed regexes)
# --------------------------------------------------------------------------- #
KEYWORD_PATTERNS = {
    "Connectivity": r"\b(connect(ion|ivity)?|not\s*connected|unable\s*to\s*connect|bad\s*connectivity)\b",
    "Device disconnection": r"\bdisconnect(ion|ing|s|ed)?\b",
    "EQ Customization": r"\b(eq|equali[sz]er)\b",
    "Login": r"\b(login|log\s*in|sign\s*in|otp)\b",
    "Location Permission": r"\b(location|gps|geolocation)\b",
    "Sound Quality": r"\b(sound|audio)(\s*quality)?\b",
    "Phone becoming silent": r"\bsilent\b",
    "Vibration Mode": r"\b(vibration\s*mode|viberation\s*mode|vibrate|vibrates)\b",
    "SKU Not supported": r"can'?t\s*connect.*app|\bnot\s*(support(ed)?|show(n|ing)|found|recogni[sz]ed|compatible)\b.{0,15}\bapp\b",
    "In Ear Detection (Dev)": r"in[\s-]*ear\s*detection|ear\s*direction",
    "Auto Pause (dev)": r"\b(auto.{0,10}pause|pause.{0,10}auto)\b",
    "Positive Feedback": r"(?<!not\s)(?<!no\s)(?<!never\s)\b(super|good|nice|best)\b",
    "Powering off (Device)": r"\b(auto\s*on\s*off|powering\s*off|power(s|ing)?\s*off)\b",
    "Crash": r"\b(crash(es|ed|ing)?|hang(s|ing)?)\b",
    "Package box (Device)": r"\b(open\s*box|package\s*box)\b",
    "Battery": r"\b(battery|battery\s*(percentage|level))\b",
    "Calibration": r"\b(calibration|3d)\b",
}
_COMPILED = {k: re.compile(v, re.I) for k, v in KEYWORD_PATTERNS.items()}


def classify_review(text):
    """Return (reason_string, consolidated_reason)."""
    found = [k for k, p in _COMPILED.items() if p.search(text or "")]
    reason = ", ".join(found) if found else "Generic Comments"
    if len(found) > 1:
        return reason, "Decision yet to be confirmed"
    return reason, (found[0] if found else "Generic Comments")
