"""Validated request model and calendar-safe duration calculations."""
from dataclasses import dataclass
from datetime import date
import re
from urllib.parse import urlsplit

EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.I)
REQUEST_ID_RE = re.compile(r"^REQ-[0-9]{8}-[A-Za-z0-9_-]{1,40}$")


def validate_product_url(value):
    value = (value or "").strip()
    if not value:
        return ""
    p = urlsplit(value)
    host = (p.hostname or "").lower()
    if (p.scheme != "https" or not host or
            not (host == "flipkart.com" or host.endswith(".flipkart.com")) or
            p.username or p.password or p.port not in (None, 443) or
            "/p/" not in p.path):
        raise ValueError("PRODUCT_URL must be an HTTPS Flipkart product page URL")
    return value


def subtract_months(day, months):
    total = day.year * 12 + day.month - 1 - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    import calendar
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def calculate_date_range(duration, unit, end_date=None):
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        raise ValueError("DURATION must be a positive integer") from None
    if duration < 1 or duration > 120:
        raise ValueError("DURATION must be between 1 and 120")
    unit = str(unit or "").strip().lower()
    end = end_date or date.today()
    if isinstance(end, str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            raise ValueError("END_DATE must use YYYY-MM-DD")
        try:
            end = date.fromisoformat(end)
        except ValueError:
            raise ValueError("END_DATE must use YYYY-MM-DD") from None
    if unit in ("week", "weeks"):
        from datetime import timedelta
        start = end - timedelta(weeks=duration)
    elif unit in ("month", "months"):
        start = subtract_months(end, duration)
    elif unit in ("year", "years"):
        start = subtract_months(end, duration * 12)
    else:
        raise ValueError("DURATION_UNIT must be weeks, months, or years")
    return start, end


@dataclass(frozen=True)
class ReportRequest:
    request_id: str
    product_url: str
    duration: int
    duration_unit: str
    recipient_email: str
    start_date: date
    end_date: date

    @classmethod
    def from_mapping(cls, data):
        get = lambda key, default="": str(data.get(key, default) or "").strip()
        rid, email = get("REQUEST_ID"), get("RECIPIENT_EMAIL")
        if not REQUEST_ID_RE.fullmatch(rid):
            raise ValueError("REQUEST_ID has an invalid format")
        if not EMAIL_RE.fullmatch(email):
            raise ValueError("recipient email is invalid")
        url = validate_product_url(get("PRODUCT_URL"))
        if not url:
            raise ValueError("PRODUCT_URL is required")
        duration = int(get("DURATION")) if get("DURATION").isdigit() else 0
        start, end = calculate_date_range(duration, get("DURATION_UNIT"), get("END_DATE") or None)
        supplied_start = get("START_DATE")
        if supplied_start:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", supplied_start):
                raise ValueError("START_DATE must use YYYY-MM-DD")
            try:
                start = date.fromisoformat(supplied_start)
            except ValueError:
                raise ValueError("START_DATE must use YYYY-MM-DD") from None
            expected_start, _ = calculate_date_range(duration, get("DURATION_UNIT"), end)
            if start != expected_start:
                raise ValueError("START_DATE does not match DURATION, DURATION_UNIT, and END_DATE")
        return cls(rid, url, duration, get("DURATION_UNIT").lower(), email, start, end)

    def as_dict(self):
        return {"request_id": self.request_id, "product_url": self.product_url, "duration": self.duration,
                "duration_unit": self.duration_unit, "recipient_email": self.recipient_email,
                "start_date": self.start_date.isoformat(), "end_date": self.end_date.isoformat()}
