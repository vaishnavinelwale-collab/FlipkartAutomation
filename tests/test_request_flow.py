import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from request_flow import ReportRequest, calculate_date_range, validate_product_url


def _request(**changes):
    data = {"REQUEST_ID": "REQ-20261022-001",
            "PRODUCT_URL": "https://www.flipkart.com/item/p/itm123", "DURATION": "3", "DURATION_UNIT": "months",
            "RECIPIENT_EMAIL": "reports@example.com", "START_DATE": "2026-07-22", "END_DATE": "2026-10-22"}
    data.update(changes)
    return data


@pytest.mark.parametrize("n,unit,end,start", [
    (3, "months", date(2026, 10, 22), date(2026, 7, 22)),
    (2, "weeks", date(2026, 10, 22), date(2026, 10, 8)),
    (1, "years", date(2024, 2, 29), date(2023, 2, 28)),
    (1, "months", date(2026, 3, 31), date(2026, 2, 28)),
])
def test_duration_calendar_math(n, unit, end, start):
    assert calculate_date_range(n, unit, end) == (start, end)


def test_request_validates_required_fields_and_consistent_date_window():
    request = ReportRequest.from_mapping(_request())
    assert request.start_date == date(2026, 7, 22)
    assert request.end_date == date(2026, 10, 22)
    with pytest.raises(ValueError, match="does not match"):
        ReportRequest.from_mapping(_request(START_DATE="2026-07-21"))
    with pytest.raises(ValueError, match="email"):
        ReportRequest.from_mapping(_request(RECIPIENT_EMAIL="not-an-email"))
    with pytest.raises(ValueError, match="between 1 and 120"):
        calculate_date_range("0", "weeks")


@pytest.mark.parametrize("url", [
    "http://www.flipkart.com/item/p/itm1", "https://flipkart.com.evil.test/item/p/itm1",
    "https://user@www.flipkart.com/item/p/itm1", "https://www.flipkart.com/item",
    "https://www.flipkart.com:444/item/p/itm1",
])
def test_rejects_unsafe_product_urls(url):
    with pytest.raises(ValueError):
        validate_product_url(url)


def test_accepts_flipkart_product_url():
    url = "https://www.flipkart.com/item/p/itm123?pid=abc"
    assert validate_product_url(url) == url


