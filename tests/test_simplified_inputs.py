"""Regression tests for URL-only product requests."""
from datetime import date
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from request_flow import ReportRequest


def _request(**changes):
    data = {
        "REQUEST_ID": "REQ-20261022-101",
        "PRODUCT_URL": "https://www.flipkart.com/item/p/itm123",
        "DURATION": "2",
        "DURATION_UNIT": "weeks",
        "RECIPIENT_EMAIL": "reports@example.com",
        "START_DATE": "2026-10-08",
        "END_DATE": "2026-10-22",
    }
    data.update(changes)
    return data


def test_product_url_is_the_only_required_product_identifier():
    request = ReportRequest.from_mapping(_request())
    assert request.product_url == "https://www.flipkart.com/item/p/itm123"
    assert "product_name" not in request.as_dict()
    assert (request.start_date, request.end_date) == (date(2026, 10, 8), date(2026, 10, 22))


def test_missing_product_url_is_rejected_even_if_legacy_product_name_is_present():
    with pytest.raises(ValueError, match="PRODUCT_URL is required"):
        ReportRequest.from_mapping(_request(PRODUCT_URL="", PRODUCT_NAME="legacy name"))


def test_product_name_is_not_required_when_product_url_is_present():
    request = ReportRequest.from_mapping(_request(PRODUCT_NAME=""))
    assert request.product_url.endswith("/p/itm123")


def test_recipient_email_remains_required_for_report_delivery():
    with pytest.raises(ValueError, match="recipient email is invalid"):
        ReportRequest.from_mapping(_request(RECIPIENT_EMAIL=""))
