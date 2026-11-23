"""
Evidence tool: explains WHY the parser found nothing in a saved diagnostic page.
Usage (Windows):  python tools\\inspect_diagnostic.py diagnostics\\boAt_Airdopes_800_reviews_20261016_152612.html
Paste the output back (or upload the .html) - it contains no credentials, only page structure.
"""
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bs4 import BeautifulSoup  # noqa: E402

from fk_common import load_config  # noqa: E402
from fk_parsing import _DATE_LEAF, _own_text, _tier1, _tier2, parse_rating_summary  # noqa: E402

BLOCK_WORDS = ["captcha", "verify you are", "unusual traffic", "access denied", "are you a robot",
               "enter your mobile", "login to continue", "please log in", "something went wrong"]


def main(path):
    html = Path(path).read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    _, selectors = load_config()
    print(f"File: {path}  ({len(html):,} bytes)")
    print("Title:", soup.title.get_text(strip=True) if soup.title else None)
    canon = soup.find("link", rel="canonical")
    print("Canonical URL:", canon.get("href") if canon else None)
    print("Blocking/verification words present:", [w for w in BLOCK_WORDS if w in text.lower()] or "none")
    print("iframes:", len(soup.find_all("iframe")), "| shadow-root markers:", html.count("shadowroot"))

    print("\n-- Tier 1 (configured selectors) --")
    for k in ("review_block", "rating", "short_review", "description", "username", "date"):
        hits = sum(len(soup.select(s)) for s in selectors.get(k, []) if s)
        print(f"  {k:13s} {selectors.get(k)} -> {hits} element(s)")
    print("  parsed records:", len(_tier1(soup, selectors)[0]))

    print("\n-- Tier 2 (layout independent) --")
    anchors = [el for el in soup.find_all(True) if _DATE_LEAF.match(_own_text(el) or "")]
    print("  date-like leaves:", len(anchors), [a.get_text(strip=True)[:25] for a in anchors[:5]])
    print("  parsed records:", len(_tier2(soup)[0]))

    print("\n-- Histogram --")
    summ = parse_rating_summary(text)
    print("  counts:", summ["counts"] or "NOT VERIFIED", "| page total:", summ["total_ratings"],
          "| verified:", summ["verified"], "| computed average:", summ["average"])

    print("\n-- Where does review-like text live? --")
    for needle in ("Certified Buyer", "READ MORE", "ago", "Helpful"):
        print(f"  '{needle}' occurrences: {text.count(needle)}")
    attrs = Counter()
    for el in soup.find_all(True):
        for a in el.attrs:
            if a.startswith("data-") or a in ("role", "itemprop", "itemtype", "aria-label"):
                attrs[a] += 1
    print("  stable-looking attributes:", dict(attrs.most_common(8)) or "none")
    for el in soup.find_all(string=re.compile(r"Certified Buyer")):
        chain, node = [], el.parent
        for _ in range(6):
            if node is None:
                break
            chain.append(f"{node.name}.{'.'.join(node.get('class', []))[:30]}")
            node = node.parent
        print("  sample ancestor chain of a 'Certified Buyer' node:", " < ".join(chain))
        break


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
