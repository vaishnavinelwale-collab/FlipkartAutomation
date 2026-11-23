#Project Name : boAt - Auto E-mail Sender
#Description  : Sends the Excel reports (and failure alerts). Credentials come from
#               environment variables / .env - NEVER hard-code passwords in source.
#Required     : SMTP_USER, SMTP_APP_PASSWORD, REPORT_TO (comma-separated)
#Optional     : SMTP_SERVER (default smtp.gmail.com), SMTP_PORT (default 587), ALERT_TO
#********************************************************************#
import logging
import smtplib
import sys
import time
from email.message import EmailMessage

from fk_common import env, load_config, resolve_dir, setup_logging

log = setup_logging()
XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _recipients(*names):
    for n in names:
        v = env(n)
        if v:
            return [x.strip() for x in v.split(",") if x.strip()]
    return []


def _send(msg, retries=3):
    user, password = env("SMTP_USER"), env("SMTP_APP_PASSWORD")
    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_APP_PASSWORD not set (see .env.example)")
    msg["From"] = user
    last = None
    for attempt in range(1, retries + 1):  # bulk legacy mode retries; request delivery is at-most-once
        try:
            with smtplib.SMTP(env("SMTP_SERVER", "smtp.gmail.com"), int(env("SMTP_PORT", "587")), timeout=60) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
            return
        except (smtplib.SMTPException, OSError) as exc:
            last = exc
            logging.getLogger("fk").warning("SMTP attempt %d failed: %s", attempt, exc)
            time.sleep(5 * attempt)
    raise RuntimeError(f"E-mail could not be sent: {last}")


class RequestDeliveryError(RuntimeError):
    def __init__(self, error_type, uncertain):
        self.delivery_uncertain = uncertain
        super().__init__(f"E-mail delivery failed ({error_type})")


def _send_request(msg, attempts=3):
    """Retry network/temporary 4xx failures only when SMTP did not accept the message."""
    user, password = env("SMTP_USER"), env("SMTP_APP_PASSWORD")
    if not user or not password:
        raise RequestDeliveryError("SMTP credentials missing", uncertain=False)
    msg["From"] = user
    for attempt in range(1, attempts + 1):
        phase = "connect"
        try:
            with smtplib.SMTP(env("SMTP_SERVER", "smtp.gmail.com"), int(env("SMTP_PORT", "587")), timeout=60) as s:
                phase = "tls"
                s.starttls()
                phase = "auth"
                s.login(user, password)
                phase = "send"
                s.send_message(msg)
            return
        except Exception as exc:
            code = getattr(exc, "smtp_code", None)
            if isinstance(exc, smtplib.SMTPRecipientsRefused):
                codes = [item[0] for item in exc.recipients.values() if item]
                safe_temporary_rejection = bool(codes) and all(400 <= int(c) < 500 for c in codes)
            else:
                safe_temporary_rejection = isinstance(exc, smtplib.SMTPDataError) and code is not None and 400 <= int(code) < 500
            safe_network_failure = phase in ("connect", "tls", "auth") and isinstance(
                exc, (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)) and not isinstance(
                exc, smtplib.SMTPAuthenticationError)
            safe_pre_send_4xx = phase in ("connect", "tls", "auth") and isinstance(
                exc, smtplib.SMTPResponseException) and code is not None and 400 <= int(code) < 500
            can_retry = attempt < attempts and (safe_network_failure or safe_temporary_rejection or safe_pre_send_4xx)
            if can_retry:
                logging.getLogger("fk").warning("Request email pre-acceptance attempt %d failed (%s); retrying",
                                                attempt, type(exc).__name__)
                time.sleep(2 * attempt)
                continue
            known_rejection = safe_temporary_rejection or isinstance(exc, (smtplib.SMTPDataError, smtplib.SMTPRecipientsRefused))
            uncertain = phase == "send" and not known_rejection
            raise RequestDeliveryError(type(exc).__name__, uncertain=uncertain) from None


def send_request_report(request_id, recipient, product, start_date, end_date, workbook, review_count, status):
    """Send exactly one request-scoped workbook, guarded by a persistent idempotency marker.

    A marker is claimed before contacting SMTP. An interrupted/uncertain attempt is intentionally
    not sent again automatically because SMTP may have accepted the message before the connection
    failed. An administrator can inspect that request's marker before any manual resend.
    """
    import json
    import os
    from pathlib import Path
    from request_flow import EMAIL_RE, REQUEST_ID_RE

    if not REQUEST_ID_RE.fullmatch(str(request_id)):
        raise ValueError("Invalid request id")
    if not EMAIL_RE.fullmatch(str(recipient or "")):
        raise ValueError("Invalid recipient email")
    file_path = Path(workbook).resolve()
    expected_parent = (Path(__file__).resolve().parent / "reports" / request_id).resolve()
    if file_path.parent != expected_parent or file_path.suffix.lower() != ".xlsx" or not file_path.is_file():
        raise ValueError("Workbook must be the existing .xlsx file inside this request's report directory")
    marker = expected_parent / ".email_delivery.json"
    record = {"request_id": request_id, "status": "sending",
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = json.loads(marker.read_text(encoding="utf-8"))
        raise RuntimeError(f"Email delivery already claimed for this request ({existing.get('status', 'unknown')})") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        msg = EmailMessage()
        msg["To"] = recipient
        msg["Subject"] = f"Flipkart review report: {product} ({start_date} to {end_date})"
        msg["X-Request-ID"] = request_id
        msg.set_content(f"Request: {request_id}\nProduct: {product}\nReview date range: {start_date} to {end_date}\n"
                        f"Reviews collected: {review_count}\nCollection status: {status}\n\n"
                        "The attached workbook contains individually extracted reviews and collection notes.")
        msg.add_attachment(file_path.read_bytes(), maintype=XLSX_MIME[0], subtype=XLSX_MIME[1], filename=file_path.name)
        try:
            _send_request(msg)
        except Exception as exc:
            record.update(status=("delivery_uncertain" if getattr(exc, "delivery_uncertain", True)
                                  else "failed_before_send"), error=type(exc).__name__)
            marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
            raise
        record.update(status="sent", completed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record
    except Exception:
        raise


def send_alert(subject, body):
    to = _recipients("ALERT_TO", "REPORT_TO")
    if not to:
        raise RuntimeError("ALERT_TO / REPORT_TO not set")
    msg = EmailMessage()
    msg["To"], msg["Subject"] = ", ".join(to), subject
    msg.set_content(body)
    _send(msg)


def _select_reports(out_dir):
    """Only attach workbooks for products whose latest run succeeded or was partial.
    Failed products are listed in the body instead of shipping stale/empty files."""
    import json
    from fk_common import BASE_DIR
    from fk_excel import safe_filename
    report = BASE_DIR / "run_report.json"
    if not report.exists():
        files = sorted(out_dir.glob("*.xlsx"))
        return files, [], "run_report.json not found - could not verify which reports are current."
    results = json.loads(report.read_text(encoding="utf-8")).get("results", [])
    ok = [out_dir / safe_filename(r["product"]) for r in results if r["status"] in ("success", "partial")]
    failed = [f"{r['product']} ({r['status']}: {r.get('failure_reason') or 'see run_report.json'})"
              for r in results if r["status"] not in ("success", "partial")]
    partial = [r["product"] for r in results if r["status"] == "partial"]
    note = f"Partial data (check warnings in run_report.json): {', '.join(partial)}" if partial else ""
    return [f for f in ok if f.exists()], failed, note


def send_reports():
    settings, _ = load_config()
    to = _recipients("REPORT_TO")
    if not to:
        raise RuntimeError("REPORT_TO not set")
    files, failed, note = _select_reports(resolve_dir(settings["output_dir"]))
    if not files:
        raise RuntimeError("No up-to-date .xlsx reports to send - run Flipkart_Reviews_Hearables.py first")
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    msg["Subject"] = "🖥🥁🎧 BFAT Flipkart 🐞 [Hearables - OnlyJk] 🎧🥁🖥"
    body = ("Hi Team,\n\nPlease find the Flipkart - Hearables Product User Feedback Report.\n\n"
            "Note: Please review the report [Edit & Update the details] then share it to all\n")
    if note:
        body += f"\n{note}\n"
    if failed:
        body += "\nNOT included (scrape failed this run):\n" + "\n".join(f"  - {f}" for f in failed) + "\n"
    msg.set_content(body + "\nThanks & Regards,\nTeam boAt R&D")
    for f in files:
        msg.add_attachment(f.read_bytes(), maintype=XLSX_MIME[0], subtype=XLSX_MIME[1], filename=f.name)
    _send(msg)
    log.info("Email with %d attachment(s) sent successfully.", len(files))


if __name__ == "__main__":
    time.sleep(2)
    try:
        send_reports()
    except Exception as exc:
        log.error("%s", exc)
        sys.exit(1)
