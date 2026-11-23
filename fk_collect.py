# # v6
# """
# Scraping pipeline that is independent of Selenium.

# It talks to a *page* object (fk_browser.LivePage in production, a fake in tests), so the
# navigation / pagination / status logic can be tested offline against saved HTML.

# Statuses (exactly one per product):
#   success            reviews extracted AND rating histogram verified
#   partial            reviews extracted, but something is missing/doubtful (see warnings)
#   no_reviews         reviews page loaded, but no review entries were found
#   navigation_failed  could not reach the reviews page (timeout / redirect / error page)
#   blocked            access restriction, verification screen or login wall
#   error              unexpected exception while processing
# """
# import logging
# import re
# import threading
# import time

# from collections import Counter

# from fk_parsing import (classify_page, date_status_text, infer_sentiment, is_sorted_recent, parse_iso_date,
#                         parse_rating_summary, parse_reviews_detailed, review_window_state)

# log = logging.getLogger("fk")

# SUCCESS, PARTIAL, NO_REVIEWS = "success", "partial", "no_reviews"
# NAV_FAILED, BLOCKED, ERROR = "navigation_failed", "blocked", "error"
# ALL_STATUSES = (SUCCESS, PARTIAL, NO_REVIEWS, NAV_FAILED, BLOCKED, ERROR)
# OK_STATUSES = (SUCCESS, PARTIAL)  # statuses that produce a usable report

# DEFAULTS = {
#     "max_reviews": 0,          # 0 = no fixed cap; stop when the requested range/list is exhausted
#     "page_size": 10,           # reviews rendered per batch
#     "max_scroll_rounds": 40,   # safety cap for infinite scroll
#     "scroll_patience": 2,      # stop after this many rounds without a new review
#     "scroll_wait_seconds": 8,  # wait for a new batch after each scroll
#     "date_stop_streak": 10,    # stop once this many reviews IN A ROW are older than start_date
    
# }

# LIMIT_STOPS = ("max_reviews", "max_scroll_rounds")


# def new_result(name, url):
#     return {
#         "product": name, "url": url, "final_url": "", "page_title": "",
#         "status": ERROR, "failure_reason": "",
#         "review_elements_detected": 0, "valid_reviews": 0, "invalid_reviews": 0,
#         "truncated_reviews": 0, "positive_reviews": 0, "negative_reviews": 0,
#         "negative_rows_written": 0,
#         "histogram_extracted": False, "histogram_verified": False,
#         "total_ratings_on_page": None, "total_reviews_on_page": None,
#         "pagination_attempted": False, "pagination_mode": "none", "scroll_rounds": 0,
#         "sorted_recent": None, "parser_tier": "",
#         "date_start": "", "date_end": "", "reviews_scanned": 0, "reviews_in_range": 0,
#         "reviews_date_uncertain": 0, "reviews_before_range": 0, "reviews_after_range": 0,
#         "reviews_undated": 0, "latest_sort_clicked": False, "stop_reason": "", "coverage": "",
#         "warnings": [], "notes": [], "diagnostics": "", "pagination_checks": [],
#     }


# def _cfg(settings, key):
#     return settings.get(key, DEFAULTS[key])


# def get_window(settings):
#     """(start, end) dates from settings['start_date'] / ['end_date'] (ISO strings), None = open."""
#     return tuple(parse_iso_date(settings[k]) if settings.get(k) else None for k in ("start_date", "end_date"))


# def collect_reviews(page, selectors, settings, base_url=None, window=None, today=None,
#                     sort_check=True, stop_at_start=True, deadline=None):
#     """Collect reviews through Flipkart's cursor-based infinite-scroll UI.

#     With a date window, scrolling continues towards older reviews until the start-date streak is
#     verified, the UI stops yielding new records, or a safety limit is reached. Direct `?page=N`
#     navigation is deliberately not used because it can repeat or omit Flipkart review batches.
#     Reviews are included only when their exact review day is confirmed inside the inclusive window.
#     """
#     start, end = window if window else (None, None)
#     filtering = bool(start or end)
#     acc = []
#     scroll_slots = {}
#     info = {"detected": 0, "invalid": 0, "rounds": 0, "attempted": False, "mode": "none",
#             "latest_clicked": False, "tier": "", "stop_reason": "", "scanned": 0, "sorted_recent": None,
#             "coverage": "not_date_filtered", "used_alternate_sort": False, "pagination_checks": []}
    
#     def _scroll_fingerprint(review):
#         """Fields used only to recognize repeated rows in cumulative scroll snapshots.

#         Occurrence numbers preserve identical records that appear more than once in the same
#         rendered snapshot. Including date/rating/variant avoids collapsing distinct reviews that
#         happen to share an author and text.
#         """
#         description = str(review.get("Review Description") or "").strip()
#         description = re.sub(r"(?:\.\.\.|…)+$", "", description).strip()
#         return tuple(str(review.get(key) or "").strip() for key in (
#             "User Name", "Ratings", "Short Review", "Date", "Variant", "Location",
#             "Helpful Votes", "Verified Purchase",
#         )) + (description[:80],)

#     def ingest(source="scroll"):
#         d = parse_reviews_detailed(page.html(), selectors, today)
#         info["detected"] = max(info["detected"], d["detected"])
#         info["invalid"] = max(info["invalid"], d["invalid"])
#         if d["tier"] != "none":
#             info["tier"] = d["tier"]
#         if source == "page":
#             # User requested every row; overlapping pages/sort orders may therefore repeat a review.
#             acc.extend(d["reviews"])
#             return d

#         snapshot_counts = Counter()
#         for review in d["reviews"]:
#             fingerprint = _scroll_fingerprint(review)
#             snapshot_counts[fingerprint] += 1
#             occurrence = snapshot_counts[fingerprint]
#             slot = (fingerprint, occurrence)
#             if slot not in scroll_slots:
#                 scroll_slots[slot] = len(acc)
#                 acc.append(review)
#             else:
#                 index = scroll_slots[slot]
#                 previous = acc[index]
#                 if len(review["Review Description"]) > len(previous["Review Description"]):
#                     acc[index] = review  # keep the expanded text for this rendered occurrence
#         return d

#     def reached_start():
#         """True when the newest-to-oldest list has clearly passed `start`."""
#         if not start:
#             return False
#         seen = acc
#         need = max(1, int(_cfg(settings, "date_stop_streak")))
#         if is_sorted_recent(seen) is False:      # order not trustworthy -> demand more evidence
#             need *= 2
#         tail = seen[-need:]
#         return len(tail) >= need and all(review_window_state(r, start, None) == "before" for r in tail)

#     max_reviews = max(0, int(_cfg(settings, "max_reviews")))
#     max_rounds = max(0, int(_cfg(settings, "max_scroll_rounds")))      # 0 = no fixed round limit

#     def rounds_exhausted():
#         return max_rounds > 0 and info["rounds"] >= max_rounds

#     def review_limit_reached():
#         return max_reviews > 0 and len(acc) >= max_reviews

#     page.expand_more()
#     first = ingest()

#     # Sort check: we want newest first. Click the 'Latest' chip unless the order is already confirmed.
#     # if first["reviews"] and is_sorted_recent(first["reviews"]) is not True:
    
#     if sort_check and first["reviews"] and is_sorted_recent(first["reviews"]) is not True:
#         if page.click_latest():
#             info["latest_clicked"] = True
#             acc.clear()
#             scroll_slots.clear()
#             page.expand_more()
#             ingest()

#     # if filtering and reached_start():
#     if filtering and stop_at_start and reached_start():
#         info["stop_reason"] = "reached_start_date"
#     # ---- Phase A: infinite scroll ---------------------------------------
#     stagnant = 0
#     t_loop = time.time()
#     recoveries = 0
#     max_recoveries = max(0, int(settings.get("list_recoveries", 2)))
#     info["recoveries"], info["incomplete"] = 0, False
#     while (not info["stop_reason"] and not review_limit_reached() and not rounds_exhausted()):
#         if _expired(deadline):
#             info["stop_reason"] = "time_limit"
#             log.warning("scroll loop stopped by time limit after %d rounds, %d reviews", info["rounds"], len(acc))
#             break
#         info["attempted"], info["mode"] = True, "infinite_scroll"
#         try:
#             prev_dom = page.date_count()
#             page.scroll_down()
#             page.wait_for_change(prev_dom, _cfg(settings, "scroll_wait_seconds"))
#             page.expand_more()
#         except Exception as exc:        # driver hiccup: keep everything collected so far, never lose the list
#             log.warning("scroll round %d interrupted: %s", info["rounds"] + 1, str(exc)[:100])
#             if "invalid session" in str(exc).lower():
#                 info["stop_reason"], info["incomplete"] = "browser_lost", True
#                 break
#             info["incomplete"] = True
#             stagnant += 1
#             info["rounds"] += 1
#             if stagnant >= _cfg(settings, "scroll_patience"):
#                 break
#             continue
#         before = len(acc)
#         try:
#             ingest()
#         except Exception as exc:  # a parse hiccup on one round must not end the run
#             log.warning("parse error during scroll round %d: %s", info["rounds"] + 1, str(exc)[:100])
#         info["rounds"] += 1
#         stagnant = 0 if len(acc) > before else stagnant + 1
#         if info["rounds"] % 5 == 0:
#             log.info("  scroll round %d: %d reviews so far, %d stagnant, %ds in this list",
#                      info["rounds"], len(acc), stagnant, time.time() - t_loop)
#         if filtering and stop_at_start and reached_start():
#             info["stop_reason"] = "reached_start_date"
#         elif stagnant >= _cfg(settings, "scroll_patience"):
#             # Premature-stop guard: if the page still shows its 'loading content' sentinel, the list has
#             # NOT ended - Flipkart was just slow. Re-trigger loading with a longer wait (reviews already
#             # collected stay in `acc`; the fingerprint slots make sure nothing is added twice).
#             more = getattr(page, "has_more", None)
#             if recoveries < max_recoveries and callable(more) and more():
#                 recoveries += 1
#                 info["recoveries"] = recoveries
#                 log.info("  list looks unfinished (loader still visible): recovery %d/%d with %d reviews kept",
#                          recoveries, max_recoveries, len(acc))
#                 try:
#                     page.nudge_scroll()
#                     page.wait_for_change(page.date_count(), float(_cfg(settings, "scroll_wait_seconds")) * 2)
#                 except Exception:
#                     pass
#                 stagnant = max(0, stagnant - 1)
#                 continue
#             break

#     if not info["stop_reason"]:
#         if review_limit_reached():
#             info["stop_reason"] = "max_reviews"
#         elif rounds_exhausted():
#             info["stop_reason"] = "max_scroll_rounds"
#         else:
#             info["stop_reason"] = "no_more_reviews_loaded"

#     # Flipkart's current public review UI uses cursor-based infinite scrolling.
#     # Direct `?page=N` URLs can repeat or omit review batches, so they are not
#     # valid evidence of pagination progress or exhaustion. If scrolling stops
#     # before the requested start date, retain a PARTIAL coverage result.

#     everything = list(acc)
#     if max_reviews:
#         everything = everything[:max_reviews]
#     info["scanned"] = len(everything)
#     info["sorted_recent"] = is_sorted_recent(everything)
#     if not filtering:
#         return everything, info

#     # ---- date filtering: by the REVIEW date, never the scraping date ----------
#     # states = Counter()
#     # reviews = []
#     # for r in everything:
#     #     st = review_window_state(r, start, end)
#     #     # A month/week/year interval fully inside the requested window is still
#     #     # uncertain: it does not confirm an exact review day.
#     #     if st == "in" and r.get("Date Precision") != "day":
#     #         st = "uncertain"
#     #     states[st] += 1
#     #     # User requires the exact displayed calendar day. A relative day label or a
#     #     # month/week/year span is not sufficient evidence for a strict date window.
#     #     if st == "in":
#     #         r["Date Status"] = date_status_text(r.get("Date Precision", "none"), st)
#     #         reviews.append(r)
    
#     include_uncertain = bool(settings.get("include_uncertain_dates", True))
#     states = Counter()
#     reviews = []
#     for r in everything:
#         # 'in'        every possible day of the review (a day, a week, a month...) lies inside the range
#         # 'uncertain' the possible days straddle the range boundary -> cannot be proven inside -> rejected
#         st = review_window_state(r, start, end)
#         states[st] += 1
#         if st == "in" or (st == "uncertain" and include_uncertain):
#             r["Date Status"] = date_status_text(r.get("Date Precision", "none"), st)
#             reviews.append(r)
#     info["uncertain_included"] = include_uncertain
#     info.update(in_range=states["in"], uncertain=states["uncertain"], before=states["before"],
#                 after=states["after"], undated=states["undated"])
#     stop, srt = info["stop_reason"], info["sorted_recent"]
#     oldest_is_before = bool(everything) and review_window_state(everything[-1], start, None) == "before"
#     if stop in LIMIT_STOPS:
#         info["coverage"] = "limit_reached"
#     elif stop == "reached_start_date":
#         info["coverage"] = "complete_to_start_date" if srt is not False else "reached_start_order_unverified"
#     elif (oldest_is_before and srt is True):
#         # A newest-first list ended right after passing the start date.
#         info["coverage"] = "complete_to_start_date"
#     else:
#         info["coverage"] = "stopped_unverified"
#     return reviews, info




# # Set by run_request when the operating system asks the process to stop: loops then finish and the
# # partial results are still written to the workbook instead of being lost.
# STOP = threading.Event()


# def _expired(deadline):
#     return STOP.is_set() or (deadline is not None and time.time() >= deadline)


# SORT_LABELS = {"MOST_RECENT": "Latest", "MOST_HELPFUL": "Most Helpful",
#                "POSITIVE_FIRST": "Positive", "NEGATIVE_FIRST": "Negative"}
# ALL_SORTS = ["MOST_RECENT", "MOST_HELPFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST"]
# GENERIC_NAMES = {"flipkartcustomer", "certifiedbuyer", "anonymous", "customer"}


# def _review_key(r):
#     """Identity key used to de-duplicate a review across different sections."""
#     desc = re.sub(
#         r"(?:\.\.\.|…)+$",
#         "",
#         str(r.get("Review Description") or "").strip()
#     ).strip()
#     return tuple(
#         str(r.get(key) or "").strip()
#         for key in ("User Name", "Ratings", "Short Review", "Location")
#     ) + (desc[:80],)


# # ---- merging reviews that appear in several topic / sort lists ------------------------------------
# def _flat(text):
#     """Lower-case letters/digits only. Topic cards sometimes lose their spaces and end in '...'."""
#     return re.sub(r"[\W_]+", "", str(text or "").replace("N/A", "").lower())


# def _text_of(r):
#     return _flat(f"{r.get('Short Review', '')} {r.get('Review Description', '')}")


# def _is_rated(r):
#     return isinstance(r.get("Ratings"), int)


# def _names_match(a, b):
#     a, b = _flat(a.get("User Name")), _flat(b.get("User Name"))
#     if not a or not b:
#         return False
#     if a == b:
#         return True
#     short, long_ = sorted((a, b), key=len)         # 'Karan Singh Cha…' vs 'Karan Singh Chauhan'
#     return len(short) >= 4 and long_.startswith(short)


# def _cities_match(a, b):
#     a, b = _flat(a.get("Location")), _flat(b.get("Location"))
#     return not a or not b or a == b


# def _same_review(a, b):
#     """True when two records (rated card or unrated topic excerpt) are the same review.

#     Different topics show different excerpts of one review, so the text only has to overlap
#     (one excerpt contained in the other card's text). For a real reviewer name, the same name + city
#     + displayed date is also accepted. Generic names such as 'Flipkart Customer' need the text overlap."""
#     if not (_names_match(a, b) and _cities_match(a, b)):
#         return False
#     ta, tb = _text_of(a), _text_of(b)
#     short, long_ = sorted((ta, tb), key=len)
#     if len(short) >= 10 and short in long_:
#         return True
#     generic = _flat(a.get("User Name")) in GENERIC_NAMES or _flat(b.get("User Name")) in GENERIC_NAMES
#     return (not generic) and a.get("Date") == b.get("Date") and a.get("Date") not in (None, "", "N/A")


# def _section_label(origins):
#     """{('Bass','MOST_RECENT'), ...} -> 'Bass: Latest, Most Helpful | Overall: Latest'."""
#     by_topic = {}
#     for topic, sort in sorted(origins, key=lambda o: (o[0] != "Overall", o[0], ALL_SORTS.index(o[1]))):
#         by_topic.setdefault(topic, []).append(SORT_LABELS.get(sort, sort))
#     return " | ".join(f"{t}: {', '.join(v)}" for t, v in by_topic.items())


# def _merge_sections(by_key, origins):
#     """Collapse everything collected into one list of unique reviews.

#     * rated cards (Overall list) are the reference records;
#     * topic excerpts that match a rated card are folded into it (they inherit its star rating);
#     * remaining excerpts of the same review from different topics are folded together;
#     * what is left has no star rating anywhere on the page: it is KEPT, with a sentiment inferred from
#       its wording and a 'Rating Source' flag (nothing is dropped).
#     Returns (reviews, stats)."""
#     stats = {"topic_cards": 0, "matched_to_rated": 0, "merged_topic_cards": 0,
#              "unrated_kept": 0, "unrated_skipped": 0}
#     rated = [r for r in by_key.values() if _is_rated(r)]
#     unrated = [r for r in by_key.values() if not _is_rated(r)]
#     stats["topic_cards"] = len(unrated)
#     sec = {id(r): set(origins.get(k, ())) for k, r in by_key.items()}

#     leftovers = []
#     for u in unrated:
#         host = next((r for r in rated if _same_review(u, r)), None)
#         if host is not None:
#             sec[id(host)] |= sec[id(u)]
#             stats["matched_to_rated"] += 1
#         else:
#             leftovers.append(u)

#     unique = []
#     for u in leftovers:
#         host = next((x for x in unique if _same_review(u, x)), None)
#         if host is None:
#             unique.append(u)
#             continue
#         sec[id(host)] |= sec[id(u)]
#         stats["merged_topic_cards"] += 1
#         if len(_text_of(u)) > len(_text_of(host)):            # keep the longest excerpt
#             for k in ("Short Review", "Review Description", "Truncated"):
#                 host[k] = u[k]

#     out = list(rated)
#     for u in unique:
#         u["Inferred Sentiment"] = infer_sentiment(f"{u.get('Short Review', '')} {u.get('Review Description', '')}")
#         u["Rating Source"] = "Inferred from review text (topic list shows no stars)"
#         stats["unrated_kept"] += 1
#         out.append(u)
#     for r in out:
#         r["Source Sections"] = _section_label(sec[id(r)])
#     return out, stats


# def collect_all_sections(page, selectors, settings, base_url=None, window=None, today=None):
#     """Visit EVERY topic chip x EVERY sort sub-tab and merge the reviews that fall in the date window.

#     Topics are whatever chips the page lists (Overall, Sound Quality, Bass, Design & Build,
#     Battery Backup, Value for Money, Call Quality, ...). Each topic has the sub-tabs
#     Most Helpful / Latest / Positive / Negative. Flipkart caps each list (~100 reviews), so only the
#     union over all lists gets close to complete; coverage is therefore reported as unverified unless
#     the Overall/Latest list alone reached the start date AND `visit_all_sections` is false.

#     Settings: multi_section_enabled (default True), visit_all_sections (default True),
#               section_sorts (default all four), multi_section_max (0 = no limit).
#     Nothing is skipped: every list is scrolled until the page itself says it has ended (or, for the
#     'Latest' lists, until the requested start date has been passed).
#     """
#     run_deadline = settings.get("deadline_epoch")
#     if run_deadline is None and settings.get("run_time_budget_seconds"):
#         run_deadline = time.time() + float(settings["run_time_budget_seconds"])
#     section_seconds = float(settings.get("max_section_seconds", 150))

#     def section_deadline():
#         d = time.time() + section_seconds if section_seconds > 0 else None
#         return min(d, run_deadline) if (d and run_deadline) else (d or run_deadline)

#     reviews, info = collect_reviews(page, selectors, settings, base_url, window, today,
#                                     deadline=section_deadline())
#     for _try in range(max(0, int(settings.get("section_retries", 2)))):      # interrupted first list: re-collect
#         if not info.get("incomplete") or info.get("stop_reason") == "browser_lost":
#             break
#         log.warning("first list was interrupted; re-collecting (kept %d reviews)", len(reviews))
#         again, info2 = collect_reviews(page, selectors, settings, base_url, window, today, sort_check=False,
#                                        deadline=section_deadline())
#         seen = {_review_key(r) for r in reviews}
#         reviews = reviews + [r for r in again if _review_key(r) not in seen]
#         info2["scanned"] = max(info.get("scanned", 0), info2.get("scanned", 0))
#         info = info2
#     start, end = window if window else (None, None)
#     needed = ("list_topics", "select_topic", "select_sort_order")

#     if (
#         not settings.get("multi_section_enabled", True)
#         or (info.get("coverage") == "complete_to_start_date" and not settings.get("visit_all_sections", True))
#         or not all(callable(getattr(page, method, None)) for method in needed)
#     ):
#         info.setdefault("sections", [])
#         return reviews, info

#     by_key, origins = {}, {}

#     def add(items, topic, sort_order):
#         new = 0
#         for review in items:
#             key = _review_key(review)
#             if key not in by_key:
#                 by_key[key] = review
#                 new += 1
#             origins.setdefault(key, set()).add((topic, sort_order))
#         return new

#     topics = page.list_topics() or []
#     if not topics:
#         topics = ["Overall"]
#         log.warning("no topic chips found on the reviews page; only the default list is collected")
#     first_topic = topics[0]
#     sorts = [x for x in settings.get("section_sorts", ALL_SORTS) if x in SORT_LABELS] or ALL_SORTS

#     add(reviews, first_topic, "MOST_RECENT")
#     sections = [{"topic": first_topic, "sort": "MOST_RECENT", "status": "collected",
#                  "scanned": info.get("scanned", 0), "kept": len(reviews), "new": len(by_key)}]

#     # Date-ordered 'Latest' lists first (they decide what is inside the date window), then the other
#     # sort tabs. If the time budget runs out, the most valuable lists are already done.
#     latest_pass = [(t, "MOST_RECENT") for t in topics if "MOST_RECENT" in sorts and t != first_topic]
#     fast = bool(settings.get("fast_mode", False))
#     if fast:
#         # Fast mode: Helpful/Positive/Negative lists are mostly OLD reviews (0 in range in real runs) and
#         # each costs minutes. Only the topics named in 'extra_sort_topics' (default Overall) get them,
#         # Negative first because negative reviews are what the report is about.
#         wanted = settings.get("extra_sort_topics", ["Overall"]) or topics
#         extra_topics = [t for t in topics if t in wanted] or [first_topic]
#         order = ["NEGATIVE_FIRST", "MOST_HELPFUL", "POSITIVE_FIRST"]
#         extra_sorts = [so for so in order if so in sorts]
#         other_pass = [(t, so) for t in extra_topics for so in extra_sorts]
#     else:
#         other_pass = [(t, so) for t in topics for so in sorts if so != "MOST_RECENT"]
#     plan = latest_pass + other_pass
#     max_sections = max(0, int(settings.get("multi_section_max", 0)))
#     if max_sections:
#         plan = plan[:max_sections]

#     current_topic_main = first_topic
#     section_settings = dict(settings)
#     section_settings["scroll_patience"] = min(int(_cfg(settings, "scroll_patience")), 2 if fast else 3)
#     # Flipkart shows ~100-140 reviews per list; 10 per batch -> ~14 rounds is the whole list.
#     extra_rounds = int(settings.get("extra_sort_max_rounds", 14))
#     other_settings = dict(section_settings)
#     if fast:                                    # only fast mode caps these lists; full mode reads them to the end
#         other_settings["max_scroll_rounds"] = min(int(_cfg(settings, "max_scroll_rounds")), extra_rounds)

#     retries = max(0, int(settings.get("section_retries", 2)))
#     total = len(plan)
#     rows = [{"topic": t, "sort": so, "status": "", "scanned": 0, "kept": 0, "new": 0} for t, so in plan]
#     sections.extend(rows)
#     results = [None] * total                     # per-list reviews, merged afterwards in PLAN order (deterministic)
    
#     def _reset_page(pg, ws):
#         try:
#             pg.driver.refresh()      # name below may differ in your class
#             time.sleep(4)
#         except Exception as exc:
#             log.warning("page reset failed: %s", str(exc)[:100])
#             ws["topic"] = None
#             ws["ok"] = False
    
  

#     def read_list(pg, ws, i):
#         """Collect ONE topic/sort list on page `pg`. `ws` is that browser's own state. Retries a list that
#         failed to load or was interrupted; reviews from earlier attempts are kept and never duplicated."""
#         topic, sort_order = plan[i]
#         row = rows[i]
#         if _expired(run_deadline):
#             row["status"] = "time_budget_exhausted"
#             return
#         log.info("list %d/%d: %s / %s", i + 2, total + 1, topic, sort_order)
#         got, lost = {}, False
#         for attempt in range(1, retries + 2):
#             try:
#                 if topic != ws["topic"] or attempt > 1:      # switch topic once per group of sorts
#                     # ws["ok"] = bool(pg.select_topic(topic))
#                     # ws["topic"] = topic
#                     ws["ok"] = bool(pg.select_topic(topic))
#                     ws["topic"] = topic if ws["ok"] else None
#                     if not ws["ok"]:
#                         log.info("topic '%s' not clickable (attempt %d)", topic, attempt)
#                     if attempt < retries + 1:
#                         _reset_page(pg, ws)
#                 if not ws["ok"]:
#                     row["status"] = "topic_not_clickable"
#                     continue
#                 if not pg.select_sort_order(sort_order):
#                     nav = getattr(pg, "last_page_navigation", {}) or {}
#                     row["status"] = nav.get("status") or "sort_not_selectable"
#                     log.info("list %s / %s not loaded (%s), attempt %d", topic, sort_order, row["status"], attempt)
#                 if attempt < retries + 1:
#                     _reset_page(pg, ws)
#                     if row["status"] in ("loaded_empty", "blocked", "login_required", "not_found"):
#                         break                                   # genuinely empty: retrying cannot help
#                     continue
#                 section_reviews, section_info = collect_reviews(
#                     pg, selectors, section_settings if sort_order == "MOST_RECENT" else other_settings,
#                     base_url, window, today, sort_check=False,
#                     stop_at_start=(sort_order == "MOST_RECENT"), deadline=section_deadline())
#                 for rv in section_reviews:                       # earlier attempts are preserved, no duplicates
#                     got.setdefault(_review_key(rv), rv)
#                 row.update(status="collected", scanned=max(row["scanned"], section_info.get("scanned", 0)),
#                            kept=len(got), stop_reason=section_info.get("stop_reason", ""),
#                            recoveries=section_info.get("recoveries", 0))
#                 log.info("section %s / %s: scanned %d, in range %d (%s)",
#                          topic, sort_order, row["scanned"], len(got), row["stop_reason"])
#                 if section_info.get("incomplete") and row["stop_reason"] != "browser_lost" and attempt <= retries:
#                     log.warning("section %s / %s was interrupted; re-collecting (attempt %d/%d), keeping %d reviews",
#                                 topic, sort_order, attempt + 1, retries + 1, len(got))
#                     continue
#                 if section_info.get("incomplete"):
#                     row["status"] = "collected_incomplete"
#                 break
#             except Exception as exc:
#                 # A failure in one section should not discard reviews from other sections.
#                 row["status"] = f"error: {str(exc)[:80]}"
#                 log.warning("section %s / %s failed (attempt %d): %s", topic, sort_order, attempt, str(exc)[:100])
#                 if "invalid session" in str(exc).lower():
#                     lost = True
#                     break
#         results[i] = list(got.values())
#         return lost

#     # ---- run the lists: several browsers pull lists from one shared queue --------------------------
#     import queue
#     todo = queue.Queue()
#     for i in range(total):
#         todo.put(i)

#     def work(pg, ws):
#         while True:
#             try:
#                 i = todo.get_nowait()
#             except queue.Empty:
#                 return
#             if read_list(pg, ws, i) and pg is not page:
#                 # this helper browser died: hand the list back so another browser reads it - never lose a list
#                 log.warning("parallel browser lost; list %s / %s goes back to the queue", *plan[i])
#                 results[i] = None
#                 rows[i].update(status="", scanned=0, kept=0, new=0)
#                 todo.put(i)
#                 return

#     workers = max(1, int(settings.get("parallel_workers", 1)))
#     threads, clones = [], []
#     if workers > 1 and callable(getattr(page, "clone", None)) and total > 1:
#         def helper(n):
#             pg = None
#             try:
#                 pg = page.clone()
#                 clones.append(pg)
#                 log.info("parallel browser %d ready", n)
#                 work(pg, {"topic": None, "ok": True})
#             except Exception as exc:                      # a helper that cannot start only means fewer workers:
#                 log.warning("parallel browser %d unavailable (%s); the other browsers take its lists", n, str(exc)[:100])
#         for n in range(1, min(workers, total + 1)):
#             t = threading.Thread(target=helper, args=(n,), daemon=True, name=f"fk-worker-{n}")
#             t.start()
#             threads.append(t)
#     work(page, {"topic": current_topic_main, "ok": True})
#     for t in threads:
#         t.join()
#     for pg in clones:
#         try:
#             pg.close()
#         except Exception:
#             pass
#     # Safety net: if every helper died mid-way, nothing may stay unread - finish leftovers on the main browser.
#     while not todo.empty():
#         work(page, {"topic": None, "ok": True})

#     for i, (topic, sort_order) in enumerate(plan):
#         if results[i] is not None:
#             rows[i]["new"] = add(results[i], topic, sort_order)

#     merged, merge_stats = _merge_sections(by_key, origins)
#     states = Counter()
#     for review in merged:
#         state = review_window_state(review, start, end)
#         states[state] += 1

#     scanned_total = info.get("scanned", 0) + sum(x["scanned"] for x in sections[1:])
#     visited = sum(1 for x in sections if x["status"] in ("collected", "collected_incomplete"))
#     info.update(
#         scanned=scanned_total,
#         in_range=states["in"],
#         uncertain=states["uncertain"],
#         sections=sections,
#         sections_planned=len(sections),
#         sections_collected_count=visited,
#         topic_merge=merge_stats,
#         coverage="multi_section_unverified",
#         time_limited=any(x["status"] == "time_budget_exhausted" or x.get("stop_reason") == "time_limit"
#                          for x in sections),
#     )
#     log.info("sections collected: %d of %d; unique reviews %d; topic excerpts: %s",
#              visited, len(sections), len(merged), merge_stats)
#     return merged, info


# def coverage_problems(res):
#     """Reasons a date-filtered collection may be incomplete (-> status 'partial')."""
#     cov, stop, start = res["coverage"], res["stop_reason"], res["date_start"] or "the start date"
#     if cov == "multi_section_unverified":
#         secs = res.get("sections_collected", [])
#         section_count = sum(1 for x in secs if x.get("status") == "collected")
#         skipped = [f"{x['topic']}/{SORT_LABELS.get(x['sort'], x['sort'])}" for x in secs
#                    if x.get("status") not in ("collected", "", None)]
#         extra = f" ({len(skipped)} skipped: {', '.join(skipped[:8])}{'...' if len(skipped) > 8 else ''})" if skipped else ""
#         return [
#             f"collected from {section_count} of {len(secs)} topic/sort sections{extra}; Flipkart may limit each list to "
#             f"about 100 reviews, so some reviews between {start} and the end date may be missing"
#         ]
#     if cov == "limit_reached":
#         return [f"stopped at safety limit '{stop}' before reaching reviews older than {start}: "
#                 f"some in-range reviews may be missing (raise the limit in fk_config.json)"]
#     if cov == "reached_start_order_unverified":
#         return [f"reached reviews older than {start} but the list was not confirmed newest-first, "
#                 f"so reviews may have been skipped"]
#     if cov == "stopped_unverified":
#         return [f"pagination stopped at '{stop}' before reaching reviews older than {start}; "
#                 f"the requested date range may be incomplete"]
#     return []


# def decide_status(res):
#     """Status for a product that produced reviews. Returns (status, [reasons])."""
#     reasons = []
#     if not res["histogram_verified"]:
#         reasons.append("rating histogram missing or its counts did not match the page total")
#     detected = max(res["review_elements_detected"], 1)
#     if res["invalid_reviews"] / detected > 0.10:
#         reasons.append(f"{res['invalid_reviews']} of {detected} review blocks failed validation")
#     if res["valid_reviews"] and res["truncated_reviews"] / res["valid_reviews"] > 0.20:
#         reasons.append(f"{res['truncated_reviews']} review texts are still truncated")
#     return (PARTIAL if reasons else SUCCESS), reasons


# def scrape_product(page, name, url, settings, selectors, today=None):
#     """Run navigation -> histogram -> reviews. Returns (result_dict, data_dict). Never raises."""
#     res = new_result(name, url)
#     data = {"summary": None, "reviews": []}
#     window = get_window(settings)
#     res["date_start"] = window[0].isoformat() if window[0] else ""
#     res["date_end"] = window[1].isoformat() if window[1] else ""
#     tag = re.sub(r"\W+", "_", name)[:30]
#     try:
#         nav = page.open_reviews(url)
#         res["final_url"], res["page_title"] = nav.get("final_url", ""), nav.get("title", "")
#         if not nav.get("reached"):
#             state, reason = classify_page(res["final_url"], res["page_title"], page.text(), 0)
#             res["status"] = BLOCKED if state in ("blocked", "login_required") else NAV_FAILED
#             res["failure_reason"] = reason if state != "unknown" else (nav.get("reason") or reason)
#             if nav.get("reason") and state not in ("blocked", "login_required"):
#                 res["failure_reason"] = nav["reason"]
#             res["diagnostics"] = page.snapshot(f"{tag}_navigation") or ""
#             return res, data

#         # Histogram is independent of reviews: a failure in one never invalidates the other.
#         try:
#             summary = parse_rating_summary(page.text())
#             data["summary"] = summary
#             res["histogram_extracted"] = bool(summary["counts"])
#             res["histogram_verified"] = bool(summary["verified"])
#             res["total_ratings_on_page"] = summary["total_ratings"]
#             res["total_reviews_on_page"] = summary["total_reviews"]
#             if not summary["counts"]:
#                 res["warnings"].append("rating histogram not found or counts do not add up to "
#                                        f"the page total {summary['total_ratings']}")
#         except Exception as exc:
#             res["warnings"].append(f"histogram parsing error: {str(exc)[:100]}")

#         try:
#             reviews, info = collect_all_sections(page, selectors, settings, nav.get("final_url"), window, today)
#         except Exception as exc:
#             log.exception("[%s] review collection crashed", name)
#             res["status"], res["failure_reason"] = ERROR, f"review extraction raised: {str(exc)[:200]}"
#             res["diagnostics"] = page.snapshot(f"{tag}_error") or ""
#             return res, data

#         res.update(review_elements_detected=info["detected"], invalid_reviews=info["invalid"],
#                    valid_reviews=len(reviews), pagination_attempted=info["attempted"],
#                    pagination_mode=info["mode"], scroll_rounds=info["rounds"],
#                    parser_tier=info["tier"], truncated_reviews=sum(1 for r in reviews if r["Truncated"]))
#         res.update(reviews_scanned=info["scanned"], stop_reason=info["stop_reason"], coverage=info["coverage"],
#                    latest_sort_clicked=info["latest_clicked"],
#                    reviews_in_range=info.get("in_range", len(reviews)),
#                    reviews_date_uncertain=info.get("uncertain", 0), reviews_before_range=info.get("before", 0),
#                    reviews_after_range=info.get("after", 0), reviews_undated=info.get("undated", 0),
#                    pagination_checks=info.get("pagination_checks", []))
#         res["sorted_recent"] = info["sorted_recent"]
#         res["sections_collected"] = info.get("sections", [])
#         if info.get("sections"):
#             m = info.get("topic_merge", {})
#             ok = info.get("sections_collected_count", 0)
#             bad = [f"{x['topic']}/{SORT_LABELS.get(x['sort'], x['sort'])}={x['status']}"
#                    for x in info["sections"] if x["status"] != "collected"]
#             res["notes"].append(
#                 f"sections visited: {ok} of {len(info['sections'])} topic/sort lists"
#                 + (f" (not collected: {'; '.join(bad[:10])}{'...' if len(bad) > 10 else ''})" if bad else "")
#                 + ("; TIME LIMIT REACHED - some lists are incomplete or were not visited"
#                    if info.get("time_limited") else "")
#                 + f"; topic excerpts without stars: {m.get('topic_cards', 0)} "
#                   f"({m.get('matched_to_rated', 0)} matched to a rated review, "
#                   f"{m.get('unrated_kept', 0)} kept with sentiment inferred from text)")
#         data["reviews"] = reviews

#         if not info["scanned"]:
#             state, reason = classify_page(page.url, page.title, page.text(), 0)
#             res["final_url"], res["page_title"] = page.url, page.title
#             if state in ("blocked", "login_required"):
#                 res["status"], res["failure_reason"] = BLOCKED, reason
#             else:
#                 res["status"] = NO_REVIEWS
#                 res["failure_reason"] = (reason if state != "unknown" else
#                                          "page loaded but no review entries could be parsed")
#             res["diagnostics"] = page.snapshot(f"{tag}_noreviews") or ""
#             return res, data

#         res["final_url"], res["page_title"] = page.url or res["final_url"], page.title or res["page_title"]
#         if res["sorted_recent"] is False:
#             res["warnings"].append("reviews are not in newest-first order")
#         total = res["total_reviews_on_page"]
#         if window[0] or window[1]:
#             res["notes"].append(
#                 f"date window {res['date_start'] or 'any'}..{res['date_end'] or 'any'}: scanned {info['scanned']} reviews "
#                 f"(of {total} on the page), {len(reviews)} kept [{info.get('in_range', 0)} within range, "
#                 # f"{info.get('uncertain', 0)} with an uncertain day excluded], "
#                 f"{info.get('uncertain', 0)} with an approximate date "
#                 f"{'kept and flagged' if info.get('uncertain_included') else 'excluded'}], "
#                 f"{info.get('before', 0)} older, {info.get('after', 0)} newer, {info.get('undated', 0)} without a usable date; "
#                 f"stopped because: {info['stop_reason']}; coverage: {info['coverage']}")
#             if info["coverage"] == "stopped_unverified":
#                 res["notes"].append("no further reviews loaded; could not confirm that every review back to the "
#                                     "start date was reached (list may simply end there, or loading stalled)")
#         elif total and len(reviews) < total:
#             res["notes"].append(f"collected {len(reviews)} of {total} reviews on the page "
#                                 f"(max_reviews={_cfg(settings, 'max_reviews')})")
#         status, reasons = decide_status(res)
#         extra = coverage_problems(res)
#         if extra:
#             status, reasons = PARTIAL, reasons + extra
#         res["status"], res["warnings"] = status, res["warnings"] + [r for r in reasons if r not in res["warnings"]]
#         if status == PARTIAL:
#             res["failure_reason"] = "; ".join(reasons)
#         return res, data
#     except Exception as exc:
#         log.exception("[%s] unexpected error", name)
#         res["status"], res["failure_reason"] = ERROR, f"unexpected error: {str(exc)[:200]}"
#         try:
#             res["diagnostics"] = page.snapshot(f"{tag}_error") or ""
#         except Exception:
#             pass
#         return res, data



# v7
"""
Scraping pipeline that is independent of Selenium.

It talks to a *page* object (fk_browser.LivePage in production, a fake in tests), so the
navigation / pagination / status logic can be tested offline against saved HTML.

Statuses (exactly one per product):
  success            reviews extracted AND rating histogram verified
  partial            reviews extracted, but something is missing/doubtful (see warnings)
  no_reviews         reviews page loaded, but no review entries were found
  navigation_failed  could not reach the reviews page (timeout / redirect / error page)
  blocked            access restriction, verification screen or login wall
  error              unexpected exception while processing
"""
import logging
import re
import threading
import time

from collections import Counter

from fk_parsing import (classify_page, date_status_text, infer_sentiment, is_sorted_recent, parse_iso_date,
                        parse_rating_summary, parse_reviews_detailed, review_window_state)

log = logging.getLogger("fk")

SUCCESS, PARTIAL, NO_REVIEWS = "success", "partial", "no_reviews"
NAV_FAILED, BLOCKED, ERROR = "navigation_failed", "blocked", "error"
ALL_STATUSES = (SUCCESS, PARTIAL, NO_REVIEWS, NAV_FAILED, BLOCKED, ERROR)
OK_STATUSES = (SUCCESS, PARTIAL)  # statuses that produce a usable report

DEFAULTS = {
    "max_reviews": 0,          # 0 = no fixed cap; stop when the requested range/list is exhausted
    "page_size": 10,           # reviews rendered per batch
    "max_scroll_rounds": 40,   # safety cap for infinite scroll
    "scroll_patience": 2,      # stop after this many rounds without a new review
    "scroll_wait_seconds": 8,  # wait for a new batch after each scroll
    "date_stop_streak": 10,    # stop once this many reviews IN A ROW are older than start_date

}

LIMIT_STOPS = ("max_reviews", "max_scroll_rounds")


def new_result(name, url):
    return {
        "product": name, "url": url, "final_url": "", "page_title": "",
        "status": ERROR, "failure_reason": "",
        "review_elements_detected": 0, "valid_reviews": 0, "invalid_reviews": 0,
        "truncated_reviews": 0, "positive_reviews": 0, "negative_reviews": 0,
        "negative_rows_written": 0,
        "histogram_extracted": False, "histogram_verified": False,
        "total_ratings_on_page": None, "total_reviews_on_page": None,
        "pagination_attempted": False, "pagination_mode": "none", "scroll_rounds": 0,
        "sorted_recent": None, "parser_tier": "",
        "date_start": "", "date_end": "", "reviews_scanned": 0, "reviews_in_range": 0,
        "reviews_date_uncertain": 0, "reviews_before_range": 0, "reviews_after_range": 0,
        "reviews_undated": 0, "latest_sort_clicked": False, "stop_reason": "", "coverage": "",
        "warnings": [], "notes": [], "diagnostics": "", "pagination_checks": [],
    }


def _cfg(settings, key):
    return settings.get(key, DEFAULTS[key])


def get_window(settings):
    """(start, end) dates from settings['start_date'] / ['end_date'] (ISO strings), None = open."""
    return tuple(parse_iso_date(settings[k]) if settings.get(k) else None for k in ("start_date", "end_date"))


def collect_reviews(page, selectors, settings, base_url=None, window=None, today=None,
                    sort_check=True, stop_at_start=True, deadline=None):
    """Collect reviews through Flipkart's cursor-based infinite-scroll UI.

    With a date window, scrolling continues towards older reviews until the start-date streak is
    verified, the UI stops yielding new records, or a safety limit is reached. Direct `?page=N`
    navigation is deliberately not used because it can repeat or omit Flipkart review batches.
    Reviews are included only when their exact review day is confirmed inside the inclusive window.
    """
    start, end = window if window else (None, None)
    filtering = bool(start or end)
    acc = []
    scroll_slots = {}
    info = {"detected": 0, "invalid": 0, "rounds": 0, "attempted": False, "mode": "none",
            "latest_clicked": False, "tier": "", "stop_reason": "", "scanned": 0, "sorted_recent": None,
            "coverage": "not_date_filtered", "used_alternate_sort": False, "pagination_checks": []}

    def _scroll_fingerprint(review):
        """Fields used only to recognize repeated rows in cumulative scroll snapshots.

        Occurrence numbers preserve identical records that appear more than once in the same
        rendered snapshot. Including date/rating/variant avoids collapsing distinct reviews that
        happen to share an author and text.
        """
        description = str(review.get("Review Description") or "").strip()
        description = re.sub(r"(?:\.\.\.|…)+$", "", description).strip()
        return tuple(str(review.get(key) or "").strip() for key in (
            "User Name", "Ratings", "Short Review", "Date", "Variant", "Location",
            "Helpful Votes", "Verified Purchase",
        )) + (description[:80],)

    def ingest(source="scroll"):
        d = parse_reviews_detailed(page.html(), selectors, today)
        info["detected"] = max(info["detected"], d["detected"])
        info["invalid"] = max(info["invalid"], d["invalid"])
        if d["tier"] != "none":
            info["tier"] = d["tier"]
        if source == "page":
            # User requested every row; overlapping pages/sort orders may therefore repeat a review.
            acc.extend(d["reviews"])
            return d

        snapshot_counts = Counter()
        for review in d["reviews"]:
            fingerprint = _scroll_fingerprint(review)
            snapshot_counts[fingerprint] += 1
            occurrence = snapshot_counts[fingerprint]
            slot = (fingerprint, occurrence)
            if slot not in scroll_slots:
                scroll_slots[slot] = len(acc)
                acc.append(review)
            else:
                index = scroll_slots[slot]
                previous = acc[index]
                if len(review["Review Description"]) > len(previous["Review Description"]):
                    acc[index] = review  # keep the expanded text for this rendered occurrence
        return d

    def reached_start():
        """True when the newest-to-oldest list has clearly passed `start`."""
        if not start:
            return False
        seen = acc
        need = max(1, int(_cfg(settings, "date_stop_streak")))
        if is_sorted_recent(seen) is False:      # order not trustworthy -> demand more evidence
            need *= 2
        tail = seen[-need:]
        return len(tail) >= need and all(review_window_state(r, start, None) == "before" for r in tail)

    max_reviews = max(0, int(_cfg(settings, "max_reviews")))
    max_rounds = max(0, int(_cfg(settings, "max_scroll_rounds")))      # 0 = no fixed round limit

    def rounds_exhausted():
        return max_rounds > 0 and info["rounds"] >= max_rounds

    def review_limit_reached():
        return max_reviews > 0 and len(acc) >= max_reviews

    page.expand_more()
    first = ingest()

    # Sort check: we want newest first. Click the 'Latest' chip unless the order is already confirmed.
    # if first["reviews"] and is_sorted_recent(first["reviews"]) is not True:

    if sort_check and first["reviews"] and is_sorted_recent(first["reviews"]) is not True:
        if page.click_latest():
            info["latest_clicked"] = True
            acc.clear()
            scroll_slots.clear()
            page.expand_more()
            ingest()

    # if filtering and reached_start():
    if filtering and stop_at_start and reached_start():
        info["stop_reason"] = "reached_start_date"
    # ---- Phase A: infinite scroll ---------------------------------------
    stagnant = 0
    t_loop = time.time()
    recoveries = 0
    max_recoveries = max(0, int(settings.get("list_recoveries", 2)))
    info["recoveries"], info["incomplete"] = 0, False
    while (not info["stop_reason"] and not review_limit_reached() and not rounds_exhausted()):
        if _expired(deadline):
            info["stop_reason"] = "time_limit"
            log.warning("scroll loop stopped by time limit after %d rounds, %d reviews", info["rounds"], len(acc))
            break
        info["attempted"], info["mode"] = True, "infinite_scroll"
        try:
            prev_dom = page.date_count()
            page.scroll_down()
            page.wait_for_change(prev_dom, _cfg(settings, "scroll_wait_seconds"))
            page.expand_more()
        except Exception as exc:        # driver hiccup: keep everything collected so far, never lose the list
            log.warning("scroll round %d interrupted: %s", info["rounds"] + 1, str(exc)[:100])
            if "invalid session" in str(exc).lower():
                info["stop_reason"], info["incomplete"] = "browser_lost", True
                break
            info["incomplete"] = True
            stagnant += 1
            info["rounds"] += 1
            if stagnant >= _cfg(settings, "scroll_patience"):
                break
            continue
        before = len(acc)
        try:
            ingest()
        except Exception as exc:  # a parse hiccup on one round must not end the run
            log.warning("parse error during scroll round %d: %s", info["rounds"] + 1, str(exc)[:100])
        info["rounds"] += 1
        stagnant = 0 if len(acc) > before else stagnant + 1
        if info["rounds"] % 5 == 0:
            log.info("  scroll round %d: %d reviews so far, %d stagnant, %ds in this list",
                     info["rounds"], len(acc), stagnant, time.time() - t_loop)
        if filtering and stop_at_start and reached_start():
            info["stop_reason"] = "reached_start_date"
        elif stagnant >= _cfg(settings, "scroll_patience"):
            # Premature-stop guard: if the page still shows its 'loading content' sentinel, the list has
            # NOT ended - Flipkart was just slow. Re-trigger loading with a longer wait (reviews already
            # collected stay in `acc`; the fingerprint slots make sure nothing is added twice).
            more = getattr(page, "has_more", None)
            if recoveries < max_recoveries and callable(more) and more():
                recoveries += 1
                info["recoveries"] = recoveries
                log.info("  list looks unfinished (loader still visible): recovery %d/%d with %d reviews kept",
                         recoveries, max_recoveries, len(acc))
                try:
                    page.nudge_scroll()
                    page.wait_for_change(page.date_count(), float(_cfg(settings, "scroll_wait_seconds")) * 2)
                except Exception:
                    pass
                stagnant = max(0, stagnant - 1)
                continue
            break

    if not info["stop_reason"]:
        if review_limit_reached():
            info["stop_reason"] = "max_reviews"
        elif rounds_exhausted():
            info["stop_reason"] = "max_scroll_rounds"
        else:
            info["stop_reason"] = "no_more_reviews_loaded"

    # Flipkart's current public review UI uses cursor-based infinite scrolling.
    # Direct `?page=N` URLs can repeat or omit review batches, so they are not
    # valid evidence of pagination progress or exhaustion. If scrolling stops
    # before the requested start date, retain a PARTIAL coverage result.

    everything = list(acc)
    if max_reviews:
        everything = everything[:max_reviews]
    info["scanned"] = len(everything)
    info["sorted_recent"] = is_sorted_recent(everything)
    if not filtering:
        return everything, info

    # ---- date filtering: by the REVIEW date, never the scraping date ----------
    include_uncertain = bool(settings.get("include_uncertain_dates", True))
    states = Counter()
    reviews = []
    for r in everything:
        # 'in'        every possible day of the review (a day, a week, a month...) lies inside the range
        # 'uncertain' the possible days straddle the range boundary -> cannot be proven inside -> rejected
        st = review_window_state(r, start, end)
        states[st] += 1
        if st == "in" or (st == "uncertain" and include_uncertain):
            r["Date Status"] = date_status_text(r.get("Date Precision", "none"), st)
            reviews.append(r)
    info["uncertain_included"] = include_uncertain
    info.update(in_range=states["in"], uncertain=states["uncertain"], before=states["before"],
                after=states["after"], undated=states["undated"])
    stop, srt = info["stop_reason"], info["sorted_recent"]
    oldest_is_before = bool(everything) and review_window_state(everything[-1], start, None) == "before"
    if stop in LIMIT_STOPS:
        info["coverage"] = "limit_reached"
    elif stop == "reached_start_date":
        info["coverage"] = "complete_to_start_date" if srt is not False else "reached_start_order_unverified"
    elif (oldest_is_before and srt is True):
        # A newest-first list ended right after passing the start date.
        info["coverage"] = "complete_to_start_date"
    else:
        info["coverage"] = "stopped_unverified"
    return reviews, info


# Set by run_request when the operating system asks the process to stop: loops then finish and the
# partial results are still written to the workbook instead of being lost.
STOP = threading.Event()


def _expired(deadline):
    return STOP.is_set() or (deadline is not None and time.time() >= deadline)


SORT_LABELS = {"MOST_RECENT": "Latest", "MOST_HELPFUL": "Most Helpful",
               "POSITIVE_FIRST": "Positive", "NEGATIVE_FIRST": "Negative"}
ALL_SORTS = ["MOST_RECENT", "MOST_HELPFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST"]
GENERIC_NAMES = {"flipkartcustomer", "certifiedbuyer", "anonymous", "customer"}

# List statuses that are final: retrying such a list cannot help (or it was collected).
FINAL_LIST_STATUSES = ("collected", "collected_incomplete", "time_budget_exhausted",
                       "loaded_empty", "blocked", "login_required", "not_found")


def _review_key(r):
    """Identity key used to de-duplicate a review across different sections."""
    desc = re.sub(
        r"(?:\.\.\.|…)+$",
        "",
        str(r.get("Review Description") or "").strip()
    ).strip()
    return tuple(
        str(r.get(key) or "").strip()
        for key in ("User Name", "Ratings", "Short Review", "Location")
    ) + (desc[:80],)


# ---- merging reviews that appear in several topic / sort lists ------------------------------------
def _flat(text):
    """Lower-case letters/digits only. Topic cards sometimes lose their spaces and end in '...'."""
    return re.sub(r"[\W_]+", "", str(text or "").replace("N/A", "").lower())


def _text_of(r):
    return _flat(f"{r.get('Short Review', '')} {r.get('Review Description', '')}")


def _is_rated(r):
    return isinstance(r.get("Ratings"), int)


def _names_match(a, b):
    a, b = _flat(a.get("User Name")), _flat(b.get("User Name"))
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)         # 'Karan Singh Cha…' vs 'Karan Singh Chauhan'
    return len(short) >= 4 and long_.startswith(short)


def _cities_match(a, b):
    a, b = _flat(a.get("Location")), _flat(b.get("Location"))
    return not a or not b or a == b


def _same_review(a, b):
    """True when two records (rated card or unrated topic excerpt) are the same review.

    Different topics show different excerpts of one review, so the text only has to overlap
    (one excerpt contained in the other card's text). For a real reviewer name, the same name + city
    + displayed date is also accepted. Generic names such as 'Flipkart Customer' need the text overlap."""
    if not (_names_match(a, b) and _cities_match(a, b)):
        return False
    ta, tb = _text_of(a), _text_of(b)
    short, long_ = sorted((ta, tb), key=len)
    if len(short) >= 10 and short in long_:
        return True
    generic = _flat(a.get("User Name")) in GENERIC_NAMES or _flat(b.get("User Name")) in GENERIC_NAMES
    return (not generic) and a.get("Date") == b.get("Date") and a.get("Date") not in (None, "", "N/A")


def _section_label(origins):
    """{('Bass','MOST_RECENT'), ...} -> 'Bass: Latest, Most Helpful | Overall: Latest'."""
    by_topic = {}
    for topic, sort in sorted(origins, key=lambda o: (o[0] != "Overall", o[0], ALL_SORTS.index(o[1]))):
        by_topic.setdefault(topic, []).append(SORT_LABELS.get(sort, sort))
    return " | ".join(f"{t}: {', '.join(v)}" for t, v in by_topic.items())


def _merge_sections(by_key, origins):
    """Collapse everything collected into one list of unique reviews.

    * rated cards (Overall list) are the reference records;
    * topic excerpts that match a rated card are folded into it (they inherit its star rating);
    * remaining excerpts of the same review from different topics are folded together;
    * what is left has no star rating anywhere on the page: it is KEPT, with a sentiment inferred from
      its wording and a 'Rating Source' flag (nothing is dropped).
    Returns (reviews, stats)."""
    stats = {"topic_cards": 0, "matched_to_rated": 0, "merged_topic_cards": 0,
             "unrated_kept": 0, "unrated_skipped": 0}
    rated = [r for r in by_key.values() if _is_rated(r)]
    unrated = [r for r in by_key.values() if not _is_rated(r)]
    stats["topic_cards"] = len(unrated)
    sec = {id(r): set(origins.get(k, ())) for k, r in by_key.items()}

    leftovers = []
    for u in unrated:
        host = next((r for r in rated if _same_review(u, r)), None)
        if host is not None:
            sec[id(host)] |= sec[id(u)]
            stats["matched_to_rated"] += 1
        else:
            leftovers.append(u)

    unique = []
    for u in leftovers:
        host = next((x for x in unique if _same_review(u, x)), None)
        if host is None:
            unique.append(u)
            continue
        sec[id(host)] |= sec[id(u)]
        stats["merged_topic_cards"] += 1
        if len(_text_of(u)) > len(_text_of(host)):            # keep the longest excerpt
            for k in ("Short Review", "Review Description", "Truncated"):
                host[k] = u[k]

    out = list(rated)
    for u in unique:
        u["Inferred Sentiment"] = infer_sentiment(f"{u.get('Short Review', '')} {u.get('Review Description', '')}")
        u["Rating Source"] = "Inferred from review text (topic list shows no stars)"
        stats["unrated_kept"] += 1
        out.append(u)
    for r in out:
        r["Source Sections"] = _section_label(sec[id(r)])
    return out, stats


def collect_all_sections(page, selectors, settings, base_url=None, window=None, today=None):
    """Visit EVERY topic chip x EVERY sort sub-tab and merge the reviews that fall in the date window.

    Topics are whatever chips the page lists (Overall, Sound Quality, Bass, Design & Build,
    Battery Backup, Value for Money, Call Quality, ...). Each topic has the sub-tabs
    Most Helpful / Latest / Positive / Negative. Flipkart caps each list (~100 reviews), so only the
    union over all lists gets close to complete; coverage is therefore reported as unverified unless
    the Overall/Latest list alone reached the start date AND `visit_all_sections` is false.

    Settings: multi_section_enabled (default True), visit_all_sections (default True),
              section_sorts (default all four), multi_section_max (0 = no limit),
              retry_failed_lists (default True: lists that could not be opened are retried once more,
              one after another on the main browser, after the parallel run),
              page_reset_wait_seconds (default 4: pause after reopening the reviews page before a retry).
    Nothing is skipped: every list is scrolled until the page itself says it has ended (or, for the
    'Latest' lists, until the requested start date has been passed).
    """
    run_deadline = settings.get("deadline_epoch")
    if run_deadline is None and settings.get("run_time_budget_seconds"):
        run_deadline = time.time() + float(settings["run_time_budget_seconds"])
    section_seconds = float(settings.get("max_section_seconds", 150))

    def section_deadline():
        d = time.time() + section_seconds if section_seconds > 0 else None
        return min(d, run_deadline) if (d and run_deadline) else (d or run_deadline)

    reviews, info = collect_reviews(page, selectors, settings, base_url, window, today,
                                    deadline=section_deadline())
    for _try in range(max(0, int(settings.get("section_retries", 2)))):      # interrupted first list: re-collect
        if not info.get("incomplete") or info.get("stop_reason") == "browser_lost":
            break
        log.warning("first list was interrupted; re-collecting (kept %d reviews)", len(reviews))
        again, info2 = collect_reviews(page, selectors, settings, base_url, window, today, sort_check=False,
                                       deadline=section_deadline())
        seen = {_review_key(r) for r in reviews}
        reviews = reviews + [r for r in again if _review_key(r) not in seen]
        info2["scanned"] = max(info.get("scanned", 0), info2.get("scanned", 0))
        info = info2
    start, end = window if window else (None, None)
    needed = ("list_topics", "select_topic", "select_sort_order")

    if (
        not settings.get("multi_section_enabled", True)
        or (info.get("coverage") == "complete_to_start_date" and not settings.get("visit_all_sections", True))
        or not all(callable(getattr(page, method, None)) for method in needed)
    ):
        info.setdefault("sections", [])
        return reviews, info

    by_key, origins = {}, {}

    def add(items, topic, sort_order):
        new = 0
        for review in items:
            key = _review_key(review)
            if key not in by_key:
                by_key[key] = review
                new += 1
            origins.setdefault(key, set()).add((topic, sort_order))
        return new

    topics = page.list_topics() or []
    if not topics:
        topics = ["Overall"]
        log.warning("no topic chips found on the reviews page; only the default list is collected")
    first_topic = topics[0]
    sorts = [x for x in settings.get("section_sorts", ALL_SORTS) if x in SORT_LABELS] or ALL_SORTS

    add(reviews, first_topic, "MOST_RECENT")
    sections = [{"topic": first_topic, "sort": "MOST_RECENT", "status": "collected",
                 "scanned": info.get("scanned", 0), "kept": len(reviews), "new": len(by_key)}]

    # Date-ordered 'Latest' lists first (they decide what is inside the date window), then the other
    # sort tabs. If the time budget runs out, the most valuable lists are already done.
    latest_pass = [(t, "MOST_RECENT") for t in topics if "MOST_RECENT" in sorts and t != first_topic]
    fast = bool(settings.get("fast_mode", False))
    if fast:
        # Fast mode: Helpful/Positive/Negative lists are mostly OLD reviews (0 in range in real runs) and
        # each costs minutes. Only the topics named in 'extra_sort_topics' (default Overall) get them,
        # Negative first because negative reviews are what the report is about.
        wanted = settings.get("extra_sort_topics", ["Overall"]) or topics
        extra_topics = [t for t in topics if t in wanted] or [first_topic]
        order = ["NEGATIVE_FIRST", "MOST_HELPFUL", "POSITIVE_FIRST"]
        extra_sorts = [so for so in order if so in sorts]
        other_pass = [(t, so) for t in extra_topics for so in extra_sorts]
    else:
        other_pass = [(t, so) for t in topics for so in sorts if so != "MOST_RECENT"]
    plan = latest_pass + other_pass
    max_sections = max(0, int(settings.get("multi_section_max", 0)))
    if max_sections:
        plan = plan[:max_sections]

    current_topic_main = first_topic
    section_settings = dict(settings)
    section_settings["scroll_patience"] = min(int(_cfg(settings, "scroll_patience")), 2 if fast else 3)
    # Flipkart shows ~100-140 reviews per list; 10 per batch -> ~14 rounds is the whole list.
    extra_rounds = int(settings.get("extra_sort_max_rounds", 14))
    other_settings = dict(section_settings)
    if fast:                                    # only fast mode caps these lists; full mode reads them to the end
        other_settings["max_scroll_rounds"] = min(int(_cfg(settings, "max_scroll_rounds")), extra_rounds)

    retries = max(0, int(settings.get("section_retries", 2)))
    total = len(plan)
    rows = [{"topic": t, "sort": so, "status": "", "scanned": 0, "kept": 0, "new": 0} for t, so in plan]
    sections.extend(rows)
    results = [None] * total                     # per-list reviews, merged afterwards in PLAN order (deterministic)

    def _reset_page(pg, ws):
        """Bring a misbehaving browser back to a clean reviews page before a retry.

        A reset leaves the page on its default (Overall) list, so the worker's remembered topic is
        cleared and the next attempt has to click its topic chip again. Never raises."""
        ws["topic"] = None
        try:
            reopened = False
            reloader = getattr(pg, "reload_reviews", None)      # LivePage: reload the saved reviews URL
            if callable(reloader):
                reopened = bool(reloader())
            opener = getattr(pg, "open_reviews", None)
            if not reopened and base_url and callable(opener):
                nav = opener(base_url)
                reopened = bool(isinstance(nav, dict) and nav.get("reached"))
            if not reopened:                                   # fall back to a plain browser refresh
                driver = getattr(pg, "driver", None)
                refresh = (getattr(driver, "refresh", None) or getattr(pg, "refresh", None)
                           or getattr(pg, "reload", None))
                if callable(refresh):
                    refresh()
                    reopened = True
            time.sleep(float(settings.get("page_reset_wait_seconds", 4)))
            log.info("reviews page %s before retry", "reopened" if reopened else "could not be reopened")
        except Exception as exc:
            log.warning("page reset failed: %s", str(exc)[:100])

    def read_list(pg, ws, i):
        """Collect ONE topic/sort list on page `pg`. `ws` is that browser's own state. Retries a list that
        failed to load or was interrupted; reviews from earlier attempts are kept and never duplicated.
        When an attempt fails because the topic/sort chip cannot be used, the reviews page is reopened
        before the next attempt (retrying at once on the same broken page cannot help)."""
        topic, sort_order = plan[i]
        row = rows[i]
        if _expired(run_deadline):
            row["status"] = "time_budget_exhausted"
            return
        log.info("list %d/%d: %s / %s", i + 2, total + 1, topic, sort_order)
        got, lost = {}, False
        for attempt in range(1, retries + 2):
            more_attempts = attempt <= retries
            try:
                if topic != ws["topic"] or attempt > 1:      # switch topic once per group of sorts
                    ws["ok"] = bool(pg.select_topic(topic))
                    # Only remember the topic when the click worked; a failed click must not make the
                    # next list believe this topic is already open.
                    ws["topic"] = topic if ws["ok"] else None
                    if not ws["ok"]:
                        log.info("topic '%s' not clickable (attempt %d)", topic, attempt)
                if not ws["ok"]:
                    row["status"] = "topic_not_clickable"
                    if more_attempts:
                        _reset_page(pg, ws)
                    continue
                if not pg.select_sort_order(sort_order):
                    nav = getattr(pg, "last_page_navigation", {}) or {}
                    row["status"] = nav.get("status") or "sort_not_selectable"
                    log.info("list %s / %s not loaded (%s), attempt %d", topic, sort_order, row["status"], attempt)
                    if row["status"] in ("loaded_empty", "blocked", "login_required", "not_found"):
                        break                                   # genuinely empty: retrying cannot help
                    if more_attempts:
                        _reset_page(pg, ws)
                    continue
                section_reviews, section_info = collect_reviews(
                    pg, selectors, section_settings if sort_order == "MOST_RECENT" else other_settings,
                    base_url, window, today, sort_check=False,
                    stop_at_start=(sort_order == "MOST_RECENT"), deadline=section_deadline())
                for rv in section_reviews:                       # earlier attempts are preserved, no duplicates
                    got.setdefault(_review_key(rv), rv)
                row.update(status="collected", scanned=max(row["scanned"], section_info.get("scanned", 0)),
                           kept=len(got), stop_reason=section_info.get("stop_reason", ""),
                           recoveries=section_info.get("recoveries", 0))
                log.info("section %s / %s: scanned %d, in range %d (%s)",
                         topic, sort_order, row["scanned"], len(got), row["stop_reason"])
                if section_info.get("incomplete") and row["stop_reason"] != "browser_lost" and attempt <= retries:
                    log.warning("section %s / %s was interrupted; re-collecting (attempt %d/%d), keeping %d reviews",
                                topic, sort_order, attempt + 1, retries + 1, len(got))
                    continue
                if section_info.get("incomplete"):
                    row["status"] = "collected_incomplete"
                break
            except Exception as exc:
                # A failure in one section should not discard reviews from other sections.
                row["status"] = f"error: {str(exc)[:80]}"
                log.warning("section %s / %s failed (attempt %d): %s", topic, sort_order, attempt, str(exc)[:100])
                if "invalid session" in str(exc).lower():
                    lost = True
                    break
        results[i] = list(got.values())
        return lost

    # ---- run the lists: several browsers pull lists from one shared queue --------------------------
    import queue
    todo = queue.Queue()
    for i in range(total):
        todo.put(i)

    def work(pg, ws):
        while True:
            try:
                i = todo.get_nowait()
            except queue.Empty:
                return
            if read_list(pg, ws, i) and pg is not page:
                # this helper browser died: hand the list back so another browser reads it - never lose a list
                log.warning("parallel browser lost; list %s / %s goes back to the queue", *plan[i])
                results[i] = None
                rows[i].update(status="", scanned=0, kept=0, new=0)
                todo.put(i)
                return

    workers = max(1, int(settings.get("parallel_workers", 1)))
    threads, clones = [], []
    if workers > 1 and callable(getattr(page, "clone", None)) and total > 1:
        def helper(n):
            pg = None
            try:
                pg = page.clone()
                clones.append(pg)
                log.info("parallel browser %d ready", n)
                work(pg, {"topic": None, "ok": True})
            except Exception as exc:                      # a helper that cannot start only means fewer workers:
                log.warning("parallel browser %d unavailable (%s); the other browsers take its lists", n, str(exc)[:100])
        for n in range(1, min(workers, total + 1)):
            t = threading.Thread(target=helper, args=(n,), daemon=True, name=f"fk-worker-{n}")
            t.start()
            threads.append(t)
    work(page, {"topic": current_topic_main, "ok": True})
    for t in threads:
        t.join()
    for pg in clones:
        try:
            pg.close()
        except Exception:
            pass
    # Safety net: if every helper died mid-way, nothing may stay unread - finish leftovers on the main browser.
    while not todo.empty():
        work(page, {"topic": None, "ok": True})

    # ---- second pass: lists that could not be opened are retried one by one on the main browser ------
    # The parallel run can leave a few lists unread (chip missing, page in a bad state). Now that the
    # other browsers are closed, try each of those once more on a freshly reopened reviews page.
    if settings.get("retry_failed_lists", True):
        failed = [i for i in range(total) if rows[i]["status"] not in FINAL_LIST_STATUSES]
        if failed and not _expired(run_deadline):
            log.info("second pass: retrying %d list(s) that could not be loaded: %s", len(failed),
                     ", ".join(f"{plan[i][0]}/{plan[i][1]}" for i in failed))
            retry_ws = {"topic": None, "ok": True}
            for i in failed:
                if _expired(run_deadline):
                    break
                _reset_page(page, retry_ws)
                if read_list(page, retry_ws, i):          # main browser lost: nothing more can be read
                    break

    for i, (topic, sort_order) in enumerate(plan):
        if results[i] is not None:
            rows[i]["new"] = add(results[i], topic, sort_order)

    merged, merge_stats = _merge_sections(by_key, origins)
    states = Counter()
    for review in merged:
        state = review_window_state(review, start, end)
        states[state] += 1

    scanned_total = info.get("scanned", 0) + sum(x["scanned"] for x in sections[1:])
    visited = sum(1 for x in sections if x["status"] in ("collected", "collected_incomplete"))
    info.update(
        scanned=scanned_total,
        in_range=states["in"],
        uncertain=states["uncertain"],
        sections=sections,
        sections_planned=len(sections),
        sections_collected_count=visited,
        topic_merge=merge_stats,
        coverage="multi_section_unverified",
        time_limited=any(x["status"] == "time_budget_exhausted" or x.get("stop_reason") == "time_limit"
                         for x in sections),
    )
    log.info("sections collected: %d of %d; unique reviews %d; topic excerpts: %s",
             visited, len(sections), len(merged), merge_stats)
    return merged, info


def coverage_problems(res):
    """Reasons a date-filtered collection may be incomplete (-> status 'partial')."""
    cov, stop, start = res["coverage"], res["stop_reason"], res["date_start"] or "the start date"
    if cov == "multi_section_unverified":
        secs = res.get("sections_collected", [])
        section_count = sum(1 for x in secs if x.get("status") == "collected")
        skipped = [f"{x['topic']}/{SORT_LABELS.get(x['sort'], x['sort'])}" for x in secs
                   if x.get("status") not in ("collected", "", None)]
        extra = f" ({len(skipped)} skipped: {', '.join(skipped[:8])}{'...' if len(skipped) > 8 else ''})" if skipped else ""
        return [
            f"collected from {section_count} of {len(secs)} topic/sort sections{extra}; Flipkart may limit each list to "
            f"about 100 reviews, so some reviews between {start} and the end date may be missing"
        ]
    if cov == "limit_reached":
        return [f"stopped at safety limit '{stop}' before reaching reviews older than {start}: "
                f"some in-range reviews may be missing (raise the limit in fk_config.json)"]
    if cov == "reached_start_order_unverified":
        return [f"reached reviews older than {start} but the list was not confirmed newest-first, "
                f"so reviews may have been skipped"]
    if cov == "stopped_unverified":
        return [f"pagination stopped at '{stop}' before reaching reviews older than {start}; "
                f"the requested date range may be incomplete"]
    return []


def decide_status(res):
    """Status for a product that produced reviews. Returns (status, [reasons])."""
    reasons = []
    if not res["histogram_verified"]:
        reasons.append("rating histogram missing or its counts did not match the page total")
    detected = max(res["review_elements_detected"], 1)
    if res["invalid_reviews"] / detected > 0.10:
        reasons.append(f"{res['invalid_reviews']} of {detected} review blocks failed validation")
    if res["valid_reviews"] and res["truncated_reviews"] / res["valid_reviews"] > 0.20:
        reasons.append(f"{res['truncated_reviews']} review texts are still truncated")
    return (PARTIAL if reasons else SUCCESS), reasons


def scrape_product(page, name, url, settings, selectors, today=None):
    """Run navigation -> histogram -> reviews. Returns (result_dict, data_dict). Never raises."""
    res = new_result(name, url)
    data = {"summary": None, "reviews": []}
    window = get_window(settings)
    res["date_start"] = window[0].isoformat() if window[0] else ""
    res["date_end"] = window[1].isoformat() if window[1] else ""
    tag = re.sub(r"\W+", "_", name)[:30]
    try:
        nav = page.open_reviews(url)
        res["final_url"], res["page_title"] = nav.get("final_url", ""), nav.get("title", "")
        if not nav.get("reached"):
            state, reason = classify_page(res["final_url"], res["page_title"], page.text(), 0)
            res["status"] = BLOCKED if state in ("blocked", "login_required") else NAV_FAILED
            res["failure_reason"] = reason if state != "unknown" else (nav.get("reason") or reason)
            if nav.get("reason") and state not in ("blocked", "login_required"):
                res["failure_reason"] = nav["reason"]
            res["diagnostics"] = page.snapshot(f"{tag}_navigation") or ""
            return res, data

        # Histogram is independent of reviews: a failure in one never invalidates the other.
        try:
            summary = parse_rating_summary(page.text())
            data["summary"] = summary
            res["histogram_extracted"] = bool(summary["counts"])
            res["histogram_verified"] = bool(summary["verified"])
            res["total_ratings_on_page"] = summary["total_ratings"]
            res["total_reviews_on_page"] = summary["total_reviews"]
            if not summary["counts"]:
                res["warnings"].append("rating histogram not found or counts do not add up to "
                                       f"the page total {summary['total_ratings']}")
        except Exception as exc:
            res["warnings"].append(f"histogram parsing error: {str(exc)[:100]}")

        try:
            reviews, info = collect_all_sections(page, selectors, settings, nav.get("final_url"), window, today)
        except Exception as exc:
            log.exception("[%s] review collection crashed", name)
            res["status"], res["failure_reason"] = ERROR, f"review extraction raised: {str(exc)[:200]}"
            res["diagnostics"] = page.snapshot(f"{tag}_error") or ""
            return res, data

        res.update(review_elements_detected=info["detected"], invalid_reviews=info["invalid"],
                   valid_reviews=len(reviews), pagination_attempted=info["attempted"],
                   pagination_mode=info["mode"], scroll_rounds=info["rounds"],
                   parser_tier=info["tier"], truncated_reviews=sum(1 for r in reviews if r["Truncated"]))
        res.update(reviews_scanned=info["scanned"], stop_reason=info["stop_reason"], coverage=info["coverage"],
                   latest_sort_clicked=info["latest_clicked"],
                   reviews_in_range=info.get("in_range", len(reviews)),
                   reviews_date_uncertain=info.get("uncertain", 0), reviews_before_range=info.get("before", 0),
                   reviews_after_range=info.get("after", 0), reviews_undated=info.get("undated", 0),
                   pagination_checks=info.get("pagination_checks", []))
        res["sorted_recent"] = info["sorted_recent"]
        res["sections_collected"] = info.get("sections", [])
        if info.get("sections"):
            m = info.get("topic_merge", {})
            ok = info.get("sections_collected_count", 0)
            bad = [f"{x['topic']}/{SORT_LABELS.get(x['sort'], x['sort'])}={x['status']}"
                   for x in info["sections"] if x["status"] != "collected"]
            res["notes"].append(
                f"sections visited: {ok} of {len(info['sections'])} topic/sort lists"
                + (f" (not collected: {'; '.join(bad[:10])}{'...' if len(bad) > 10 else ''})" if bad else "")
                + ("; TIME LIMIT REACHED - some lists are incomplete or were not visited"
                   if info.get("time_limited") else "")
                + f"; topic excerpts without stars: {m.get('topic_cards', 0)} "
                  f"({m.get('matched_to_rated', 0)} matched to a rated review, "
                  f"{m.get('unrated_kept', 0)} kept with sentiment inferred from text)")
        data["reviews"] = reviews

        if not info["scanned"]:
            state, reason = classify_page(page.url, page.title, page.text(), 0)
            res["final_url"], res["page_title"] = page.url, page.title
            if state in ("blocked", "login_required"):
                res["status"], res["failure_reason"] = BLOCKED, reason
            else:
                res["status"] = NO_REVIEWS
                res["failure_reason"] = (reason if state != "unknown" else
                                         "page loaded but no review entries could be parsed")
            res["diagnostics"] = page.snapshot(f"{tag}_noreviews") or ""
            return res, data

        res["final_url"], res["page_title"] = page.url or res["final_url"], page.title or res["page_title"]
        if res["sorted_recent"] is False:
            res["warnings"].append("reviews are not in newest-first order")
        total = res["total_reviews_on_page"]
        if window[0] or window[1]:
            res["notes"].append(
                f"date window {res['date_start'] or 'any'}..{res['date_end'] or 'any'}: scanned {info['scanned']} reviews "
                f"(of {total} on the page), {len(reviews)} kept [{info.get('in_range', 0)} within range, "
                f"{info.get('uncertain', 0)} with an approximate date "
                f"{'kept and flagged' if info.get('uncertain_included') else 'excluded'}], "
                f"{info.get('before', 0)} older, {info.get('after', 0)} newer, {info.get('undated', 0)} without a usable date; "
                f"stopped because: {info['stop_reason']}; coverage: {info['coverage']}")
            if info["coverage"] == "stopped_unverified":
                res["notes"].append("no further reviews loaded; could not confirm that every review back to the "
                                    "start date was reached (list may simply end there, or loading stalled)")
        elif total and len(reviews) < total:
            res["notes"].append(f"collected {len(reviews)} of {total} reviews on the page "
                                f"(max_reviews={_cfg(settings, 'max_reviews')})")
        status, reasons = decide_status(res)
        extra = coverage_problems(res)
        if extra:
            status, reasons = PARTIAL, reasons + extra
        res["status"], res["warnings"] = status, res["warnings"] + [r for r in reasons if r not in res["warnings"]]
        if status == PARTIAL:
            res["failure_reason"] = "; ".join(reasons)
        return res, data
    except Exception as exc:
        log.exception("[%s] unexpected error", name)
        res["status"], res["failure_reason"] = ERROR, f"unexpected error: {str(exc)[:200]}"
        try:
            res["diagnostics"] = page.snapshot(f"{tag}_error") or ""
        except Exception:
            pass
        return res, data


