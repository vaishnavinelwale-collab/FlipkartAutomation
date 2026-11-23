"""URL-only product request tests."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from product_resolver import ProductNotFoundError, ProductResolutionError, resolve_product
from request_flow import ReportRequest


def _request(**changes):
    data = {
        "REQUEST_ID": "REQ-20261022-111",
        "PRODUCT_URL": "https://www.flipkart.com/audio/p/itm123",
        "DURATION": "2",
        "DURATION_UNIT": "weeks",
        "RECIPIENT_EMAIL": "reports@example.com",
        "START_DATE": "2026-10-08",
        "END_DATE": "2026-10-22",
    }
    data.update(changes)
    return data


class FakeDriver:
    current_url = ""
    title = "boAt Airdopes 800 | Flipkart"

    def get(self, url):
        self.current_url = url
        self.visited = url

    def execute_script(self, _script):
        return "complete"


def test_request_accepts_url_without_product_name():
    request = ReportRequest.from_mapping(_request())
    assert request.product_url == "https://www.flipkart.com/audio/p/itm123"
    assert "product_name" not in request.as_dict()


def test_product_name_is_taken_from_the_direct_product_page_title():
    driver = FakeDriver()
    title, url = resolve_product(driver, "https://www.flipkart.com/audio/p/itm123")
    assert title == "boAt Airdopes 800"
    assert url == driver.visited


def test_resolution_fails_if_flipkart_does_not_provide_a_product_title():
    driver = FakeDriver()
    driver.title = ""
    with pytest.raises(ProductNotFoundError, match="Could not read the product title"):
        resolve_product(driver, "https://www.flipkart.com/audio/p/itm123")


def test_resolution_rejects_redirect_to_another_domain():
    driver = FakeDriver()

    def redirect(_url):
        driver.current_url = "https://example.com/product"

    driver.get = redirect
    with pytest.raises(ProductResolutionError, match="non-product page"):
        resolve_product(driver, "https://www.flipkart.com/audio/p/itm123")
