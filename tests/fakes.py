"""Offline stand-ins: synthetic pages that copy the REAL Flipkart review structure
(rating | • | title | Review for | text | name | , city | Helpful | Verified Purchase | · N days ago)."""
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SUMMARY = ("31,635 ratings and 2,726 reviews", [(1, "3,002"), (2, "1,264"), (3, "2,511"), (4, "6,271"), (5, "18,587")])


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def summary_html(with_histogram=True, total=SUMMARY[0]):
    h = f'<div dir="auto">{total}</div>'
    if with_histogram:
        h += "".join(f'<div class="r"><div>{s}</div><div>★</div><div>{c}</div></div>' for s, c in SUMMARY[1])
    return h


def review_html(i, truncated=False, rating=None, date_text=None, ident=None):
    """`date_text` overrides the displayed date; `ident` makes review i look like review `ident`
    (same name/title/text), which is how tests create duplicates."""
    rating = rating or (1 + i % 5)
    n = i if ident is None else ident
    text = f"Review body number {n} about sound and battery"
    body = (f'<div><span class="css-1jxf684">{text}...</span> <span class="css-x">more</span></div>'
            if truncated else f'<div dir="auto">{text}</div>')
    return (f'<div class="css-g5y9jx"><div dir="auto">{rating}.0</div><div dir="auto">•</div>'
            f'<div dir="auto">Title {n}</div><div dir="auto">Review for: Color Black • Microphone Yes</div>{body}'
            f'<div dir="auto">User {n}</div><div dir="auto">, City{n}</div><div dir="auto">Helpful</div>'
            f'<div dir="auto">Verified Purchase</div><div dir="auto">· {date_text or f"{i + 1} days ago"}</div></div>')


def page_html(n_reviews, with_histogram=True, truncated=(), title="Reviews | Flipkart.com",
              date_fn=None, ident_fn=None):
    reviews = "".join(review_html(i, i in truncated, date_text=date_fn(i) if date_fn else None,
                                  ident=ident_fn(i) if ident_fn else None) for i in range(n_reviews))
    return (f"<html><head><title>{title}</title></head><body>"
            f"{summary_html(with_histogram)}<div>User reviews sorted by Latest</div>{reviews}"
            f"<div>Hang on, loading content</div></body></html>")


def text_of(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser").get_text("\n")


class FakePage:
    """Implements the same interface as fk_browser.LivePage."""

    def __init__(self, total=35, batch=10, mode="scroll", reached=True, final_url=None,
                 title="Reviews | Flipkart.com", static_html=None, with_histogram=True,
                 truncated_until_expand=(), nav_reason="", raise_on_html=False,
                 date_fn=None, ident_fn=None):
        self.total, self.batch, self.mode = total, batch, mode
        self.shown = batch if total else 0
        self._reached, self._title = reached, title
        self._final = final_url or "https://www.flipkart.com/x/product-reviews/itm1?pid=A"
        self.static_html, self.with_histogram = static_html, with_histogram
        self.truncated, self.expanded = set(truncated_until_expand), False
        self.nav_reason, self.raise_on_html = nav_reason, raise_on_html
        self.page_no, self.snapshots, self.scrolls = 1, [], 0
        self.date_fn, self.ident_fn = date_fn, ident_fn

    url = property(lambda s: s._final)
    title = property(lambda s: s._title)

    def _html(self):
        if self.static_html is not None:
            return self.static_html
        if self.mode == "pages":           # ?page=N serves a distinct batch, scrolling does nothing
            lo, hi = (self.page_no - 1) * self.batch, min(self.page_no * self.batch, self.total)
            reviews = "".join(review_html(i, date_text=self.date_fn(i) if self.date_fn else None,
                                          ident=self.ident_fn(i) if self.ident_fn else None)
                              for i in range(lo, max(lo, hi)))
            return (f"<html><head><title>{self._title}</title></head><body>"
                    f"{summary_html(self.with_histogram)}{reviews}</body></html>")
        trunc = set() if self.expanded else self.truncated
        return page_html(min(self.shown, self.total), self.with_histogram, trunc, self._title,
                         self.date_fn, self.ident_fn)

    def html(self):
        if self.raise_on_html:
            raise RuntimeError("browser crashed")
        return self._html()

    def text(self):
        return text_of(self._html())

    def date_count(self):
        return min(self.shown, self.total) if self.mode == "scroll" else (self.batch if self.static_html is None else 10)

    def expand_more(self):
        n = 0 if self.expanded or not self.truncated else len(self.truncated)
        self.expanded = self.expanded or bool(self.truncated)
        return n

    def scroll_down(self):
        self.scrolls += 1
        if self.mode == "scroll":
            self.shown = min(self.shown + self.batch, self.total)

    def wait_for_change(self, prev, timeout):
        return self.date_count() != prev

    def click_latest(self):
        return True

    def select_sort_order(self, sort_order):
        return self.mode == "pages"

    def goto_page(self, url):
        import re
        m = re.search(r"page=(\d+)", url)
        self.page_no = int(m.group(1)) if m else 1
        return self.mode == "pages"

    def snapshot(self, tag):
        self.snapshots.append(tag)
        return f"diagnostics/{tag}"

    def open_reviews(self, product_url):
        return {"reached": self._reached, "final_url": self._final, "title": self._title,
                "reason": self.nav_reason, "reviews_in_dom": self.date_count()}
