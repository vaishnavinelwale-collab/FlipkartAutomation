# # # """Anonymous diagnostic for the multi-section scraper.

# # # Opens the reviews page, then visits EVERY topic chip x EVERY sort sub-tab
# # # (Most Helpful / Latest / Positive / Negative) and prints what the review parser sees in each one.
# # # Any list that parses to 0 reviews is saved (HTML + screenshot) to the 'diagnostics' folder; send those
# # # files if a section does not parse.

# # # Run from the project folder:
# # #     .\\.venv\\Scripts\\python.exe .\\diagnose_topic_section.py [product_url]
# # # It never logs in and never reads OTPs.
# # # """
# # # import sys
# # # import time
# # # from datetime import datetime
# # # from pathlib import Path

# # # from fk_browser import LivePage, dump_diagnostics, make_driver
# # # from fk_common import load_config
# # # from fk_parsing import parse_reviews_detailed

# # # PRODUCT_URL = ("https://www.flipkart.com/boat-airdopes-161-adv-4mic-enx-50h-battery-fast-charge-13mm-driver-"
# # #                "metallic-finish-bluetooth/p/itmc28324fa1e1dd?pid=ACCHY8GFZUFNZ92H&marketplace=FLIPKART")
# # # SORTS = ["MOST_HELPFUL", "MOST_RECENT", "POSITIVE_FIRST", "NEGATIVE_FIRST"]


# # # def main():
# # #     url = sys.argv[1] if len(sys.argv) > 1 else PRODUCT_URL
# # #     settings, selectors = load_config()
# # #     diag = Path(__file__).resolve().parent / "diagnostics"
# # #     diag.mkdir(exist_ok=True)
# # #     driver = make_driver(False, None)
# # #     rows = []
# # #     try:
# # #         page = LivePage(driver, settings, diag, selectors)
# # #         nav = page.open_reviews(url)
# # #         print("REACHED_REVIEWS_PAGE=", nav.get("reached"))
# # #         topics = page.list_topics()
# # #         print("TOPICS_FOUND=", topics)
# # #         for topic in topics:
# # #             clicked = page.select_topic(topic)
# # #             print(f"\nTOPIC={topic} clicked={clicked}")
# # #             for sort in SORTS:
# # #                 ok = page.select_sort_order(sort)
# # #                 time.sleep(1)
# # #                 page.scroll_down()
# # #                 time.sleep(3)
# # #                 parsed = parse_reviews_detailed(page.html(), selectors)
# # #                 n = len(parsed["reviews"])
# # #                 first = parsed["reviews"][0] if parsed["reviews"] else {}
# # #                 print(f"  {sort:15} selected={ok!s:5} reviews={n:3} tier={parsed['tier']:11} "
# # #                       f"first={first.get('User Name')!r} rating={first.get('Ratings')!r} date={first.get('Date')!r}")
# # #                 rows.append((topic, sort, ok, n))
# # #                 if n == 0:
# # #                     print("   SAVED=", dump_diagnostics(driver, diag, f"{topic}_{sort}".replace(" ", "_").replace("&", "and")))
# # #     finally:
# # #         driver.quit()
# # #     empty = [(t, s) for t, s, _, n in rows if n == 0]
# # #     print(f"\nSUMMARY lists={len(rows)} empty={len(empty)} {empty}")
# # #     print("DONE", datetime.now().isoformat(timespec="seconds"))


# # # if __name__ == "__main__":
# # #     main()





# # """Anonymous diagnostic for the multi-section scraper.

# # Opens the reviews page, then visits EVERY topic chip x EVERY sort sub-tab
# # (Most Helpful / Latest / Positive / Negative) and prints what the review parser sees in each one.
# # Any list that parses to 0 reviews is saved (HTML + screenshot) to the 'diagnostics' folder; send those
# # files if a section does not parse.

# # Run from the project folder:
# #     .\\.venv\\Scripts\\python.exe .\\diagnose_topic_section.py [product_url]
# # It never logs in and never reads OTPs.
# # """
# # import sys
# # import time
# # from datetime import datetime
# # from pathlib import Path

# # from fk_browser import LivePage, dump_diagnostics, make_driver
# # from fk_common import load_config
# # from fk_parsing import parse_reviews_detailed

# # PRODUCT_URL = ("https://www.flipkart.com/boat-airdopes-161-adv-4mic-enx-50h-battery-fast-charge-13mm-driver-"
# #                "metallic-finish-bluetooth/p/itmc28324fa1e1dd?pid=ACCHY8GFZUFNZ92H&marketplace=FLIPKART")
# # SORTS = ["MOST_HELPFUL", "MOST_RECENT", "POSITIVE_FIRST", "NEGATIVE_FIRST"]


# # def main():
# #     url = sys.argv[1] if len(sys.argv) > 1 else PRODUCT_URL
# #     settings, selectors = load_config()
# #     diag = Path(__file__).resolve().parent / "diagnostics"
# #     diag.mkdir(exist_ok=True)
# #     driver = make_driver(False, None)
# #     rows = []
# #     try:
# #         page = LivePage(driver, settings, diag, selectors)
# #         nav = page.open_reviews(url)
# #         print("REACHED_REVIEWS_PAGE=", nav.get("reached"))
# #         topics = page.list_topics()
# #         print("TOPICS_FOUND=", topics)
# #         for topic in topics:
# #             clicked = page.select_topic(topic)
# #             print(f"\nTOPIC={topic} clicked={clicked}")
# #             for sort in SORTS:
# #                 ok = page.select_sort_order(sort)
# #                 time.sleep(1)
# #                 page.scroll_down()
# #                 time.sleep(3)
# #                 parsed = parse_reviews_detailed(page.html(), selectors)
# #                 n = len(parsed["reviews"])
# #                 first = parsed["reviews"][0] if parsed["reviews"] else {}
# #                 print(f"  {sort:15} selected={ok!s:5} reviews={n:3} tier={parsed['tier']:11} "
# #                       f"first={first.get('User Name')!r} rating={first.get('Ratings')!r} date={first.get('Date')!r}")
# #                 rows.append((topic, sort, ok, n))
# #                 if n == 0:
# #                     print("   SAVED=", dump_diagnostics(driver, diag, f"{topic}_{sort}".replace(" ", "_").replace("&", "and")))
# #     finally:
# #         driver.quit()
# #     empty = [(t, s) for t, s, _, n in rows if n == 0]
# #     print(f"\nSUMMARY lists={len(rows)} empty={len(empty)} {empty}")
# #     print("DONE", datetime.now().isoformat(timespec="seconds"))


# # if __name__ == "__main__":
# #     main()




# # v4

# """Anonymous diagnostic for the multi-section scraper.

# Opens the reviews page, then visits EVERY topic chip x EVERY sort sub-tab
# (Most Helpful / Latest / Positive / Negative) and prints what the review parser sees in each one.
# Any list that parses to 0 reviews is saved (HTML + screenshot) to the 'diagnostics' folder; send those
# files if a section does not parse.

# Run from the project folder:
#     .\\.venv\\Scripts\\python.exe .\\diagnose_topic_section.py [product_url]
# It never logs in and never reads OTPs.
# """
# import sys
# import time
# from datetime import datetime
# from pathlib import Path

# from fk_browser import LivePage, dump_diagnostics, make_driver
# from fk_common import load_config
# from fk_parsing import parse_reviews_detailed

# PRODUCT_URL = ("https://www.flipkart.com/boat-airdopes-161-adv-4mic-enx-50h-battery-fast-charge-13mm-driver-"
#                "metallic-finish-bluetooth/p/itmc28324fa1e1dd?pid=ACCHY8GFZUFNZ92H&marketplace=FLIPKART")
# SORTS = ["MOST_HELPFUL", "MOST_RECENT", "POSITIVE_FIRST", "NEGATIVE_FIRST"]


# def main():
#     url = sys.argv[1] if len(sys.argv) > 1 else PRODUCT_URL
#     settings, selectors = load_config()
#     diag = Path(__file__).resolve().parent / "diagnostics"
#     diag.mkdir(exist_ok=True)
#     driver = make_driver(False, None)
#     rows = []
#     try:
#         page = LivePage(driver, settings, diag, selectors)
#         nav = page.open_reviews(url)
#         print("REACHED_REVIEWS_PAGE=", nav.get("reached"))
#         topics = page.list_topics()
#         print("TOPICS_FOUND=", topics)
#         for topic in topics:
#             clicked = page.select_topic(topic)
#             print(f"\nTOPIC={topic} clicked={clicked}")
#             for sort in SORTS:
#                 ok = page.select_sort_order(sort)
#                 time.sleep(1)
#                 page.scroll_down()
#                 time.sleep(3)
#                 parsed = parse_reviews_detailed(page.html(), selectors)
#                 n = len(parsed["reviews"])
#                 first = parsed["reviews"][0] if parsed["reviews"] else {}
#                 print(f"  {sort:15} selected={ok!s:5} reviews={n:3} tier={parsed['tier']:11} "
#                       f"first={first.get('User Name')!r} rating={first.get('Ratings')!r} date={first.get('Date')!r}")
#                 rows.append((topic, sort, ok, n))
#                 if n == 0:
#                     print("   SAVED=", dump_diagnostics(driver, diag, f"{topic}_{sort}".replace(" ", "_").replace("&", "and")))
#     finally:
#         driver.quit()
#     empty = [(t, s) for t, s, _, n in rows if n == 0]
#     print(f"\nSUMMARY lists={len(rows)} empty={len(empty)} {empty}")
#     print("DONE", datetime.now().isoformat(timespec="seconds"))


# if __name__ == "__main__":
#     main()




# v6
"""Anonymous diagnostic for the multi-section scraper.

Opens the reviews page, then visits EVERY topic chip x EVERY sort sub-tab
(Most Helpful / Latest / Positive / Negative) and prints what the review parser sees in each one.
Any list that parses to 0 reviews is saved (HTML + screenshot) to the 'diagnostics' folder; send those
files if a section does not parse.

Run from the project folder:
    .\\.venv\\Scripts\\python.exe .\\diagnose_topic_section.py [product_url]
It never logs in and never reads OTPs.
"""
import sys
import time
from datetime import datetime
from pathlib import Path

from fk_browser import LivePage, dump_diagnostics, make_driver
from fk_common import load_config
from fk_parsing import parse_reviews_detailed

PRODUCT_URL = ("https://www.flipkart.com/boat-airdopes-161-adv-4mic-enx-50h-battery-fast-charge-13mm-driver-"
               "metallic-finish-bluetooth/p/itmc28324fa1e1dd?pid=ACCHY8GFZUFNZ92H&marketplace=FLIPKART")
SORTS = ["MOST_HELPFUL", "MOST_RECENT", "POSITIVE_FIRST", "NEGATIVE_FIRST"]


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else PRODUCT_URL
    settings, selectors = load_config()
    diag = Path(__file__).resolve().parent / "diagnostics"
    diag.mkdir(exist_ok=True)
    driver = make_driver(False, None)
    rows = []
    try:
        page = LivePage(driver, settings, diag, selectors)
        nav = page.open_reviews(url)
        print("REACHED_REVIEWS_PAGE=", nav.get("reached"))
        topics = page.list_topics()
        print("TOPICS_FOUND=", topics)
        for topic in topics:
            clicked = page.select_topic(topic)
            print(f"\nTOPIC={topic} clicked={clicked}")
            for sort in SORTS:
                ok = page.select_sort_order(sort)
                time.sleep(1)
                page.scroll_down()
                time.sleep(3)
                parsed = parse_reviews_detailed(page.html(), selectors)
                n = len(parsed["reviews"])
                first = parsed["reviews"][0] if parsed["reviews"] else {}
                print(f"  {sort:15} selected={ok!s:5} reviews={n:3} tier={parsed['tier']:11} "
                      f"first={first.get('User Name')!r} rating={first.get('Ratings')!r} date={first.get('Date')!r}")
                rows.append((topic, sort, ok, n))
                if n == 0:
                    print("   SAVED=", dump_diagnostics(driver, diag, f"{topic}_{sort}".replace(" ", "_").replace("&", "and")))
    finally:
        driver.quit()
    empty = [(t, s) for t, s, _, n in rows if n == 0]
    print(f"\nSUMMARY lists={len(rows)} empty={len(empty)} {empty}")
    print("DONE", datetime.now().isoformat(timespec="seconds"))


if __name__ == "__main__":
    main()

