"""Resolve a product directly from its Flipkart listing URL."""

from request_flow import validate_product_url


class ProductResolutionError(ValueError):
    pass


class ProductNotFoundError(ProductResolutionError):
    pass


def resolve_product(driver, product_url, timeout=20):
    """Open the supplied product URL and use Flipkart's page title as the product name."""
    from selenium.webdriver.support.ui import WebDriverWait
    product_url = validate_product_url(product_url)
    if not product_url:
        raise ProductNotFoundError("A Flipkart product URL is required")
    driver.get(product_url)
    WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")
    try:
        final_url = validate_product_url(driver.current_url)
    except ValueError:
        raise ProductResolutionError("Flipkart redirected the supplied URL to a non-product page") from None
    title = " ".join((driver.title or "").split()).split(" | Flipkart")[0].strip()
    if not title:
        raise ProductNotFoundError("Could not read the product title from the supplied Flipkart URL")
    return title, final_url
