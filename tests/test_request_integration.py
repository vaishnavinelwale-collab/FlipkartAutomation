import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_request_folders_are_isolated(tmp_path, monkeypatch):
    import run_request
    monkeypatch.setattr(run_request, "BASE_DIR", tmp_path)
    first = run_request._safe_request_dir("REQ-20261022-001")
    second = run_request._safe_request_dir("REQ-20261022-002")
    (first / "report.xlsx").write_bytes(b"one")
    (second / "report.xlsx").write_bytes(b"two")
    assert first != second
    assert (first / "report.xlsx").read_bytes() == b"one"
    assert (second / "report.xlsx").read_bytes() == b"two"
    with pytest.raises(ValueError):
        run_request._safe_request_dir("../outside")


def test_request_parameter_handling_reads_environment(monkeypatch):
    import run_request
    captured = {}
    monkeypatch.setattr(run_request, "run", lambda data: captured.update(data) or 0)
    for key, value in {"REQUEST_ID": "REQ-20261022-001",
                       "PRODUCT_URL": "https://www.flipkart.com/item/p/itm123", "DURATION": "2", "DURATION_UNIT": "weeks",
                       "RECIPIENT_EMAIL": "x@example.com", "START_DATE": "2026-10-08",
                       "END_DATE": "2026-10-22"}.items():
        monkeypatch.setenv(key, value)
    assert run_request.main(["--from-env"]) == 0
    assert captured["REQUEST_ID"] == "REQ-20261022-001"
    assert captured["PRODUCT_URL"] == "https://www.flipkart.com/item/p/itm123"
    assert captured["END_DATE"] == "2026-10-22"


def test_invalid_request_is_recorded_as_failed(tmp_path, monkeypatch):
    import run_request
    monkeypatch.setattr(run_request, "BASE_DIR", tmp_path)
    monkeypatch.setattr(run_request, "_alert_admin", lambda *args: None)
    for key, value in {"REQUEST_ID": "REQ-20261022-003",
                       "PRODUCT_URL": "https://www.flipkart.com/item/p/itm123", "DURATION": "2", "DURATION_UNIT": "weeks",
                       "RECIPIENT_EMAIL": "person@example.com", "START_DATE": "2026-10-07",
                       "END_DATE": "2026-10-22"}.items():
        monkeypatch.setenv(key, value)
    assert run_request.main(["--from-env"]) == 1
    status = json.loads((tmp_path / "reports" / "REQ-20261022-003" / "request_status.json").read_text())
    assert status["status"] == "Failed" and "does not match" in status["error"]


def test_request_email_attaches_only_its_workbook_and_is_idempotent(tmp_path, monkeypatch):
    import Auto_Email_Flipkart_OnlyJk as mailer
    monkeypatch.setattr(mailer, "__file__", str(tmp_path / "mailer.py"))
    sent = []
    monkeypatch.setattr(mailer, "_send_request", lambda message, attempts=3: sent.append(message))
    reports = tmp_path / "reports"
    request_a = reports / "REQ-20261022-001"
    request_b = reports / "REQ-20261022-002"
    request_a.mkdir(parents=True)
    request_b.mkdir(parents=True)
    workbook_a = request_a / "Product_A.xlsx"
    workbook_b = request_b / "Product_B.xlsx"
    workbook_a.write_bytes(b"workbook-a")
    workbook_b.write_bytes(b"workbook-b")
    delivery = mailer.send_request_report("REQ-20261022-001", "a@example.com", "Product A",
                                          "2026-10-01", "2026-10-22", workbook_a, 4, "success")
    assert delivery["status"] == "sent"
    assert len(sent) == 1 and sent[0]["To"] == "a@example.com"
    attachments = list(sent[0].iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_payload(decode=True) == b"workbook-a"
    assert "Product_B.xlsx" not in str(sent[0])
    with pytest.raises(RuntimeError, match="already claimed"):
        mailer.send_request_report("REQ-20261022-001", "a@example.com", "Product A",
                                  "2026-10-01", "2026-10-22", workbook_a, 4, "success")
    assert json.loads((request_a / ".email_delivery.json").read_text())["status"] == "sent"


def test_uncertain_smtp_failure_is_not_automatically_retried(tmp_path, monkeypatch):
    import Auto_Email_Flipkart_OnlyJk as mailer
    monkeypatch.setattr(mailer, "__file__", str(tmp_path / "mailer.py"))
    monkeypatch.setattr(mailer, "_send_request", lambda message, attempts=3: (_ for _ in ()).throw(
        mailer.RequestDeliveryError("SMTPServerDisconnected", uncertain=True)))
    request_dir = tmp_path / "reports" / "REQ-20261022-004"
    request_dir.mkdir(parents=True)
    workbook = request_dir / "Product.xlsx"
    workbook.write_bytes(b"workbook")
    args = ("REQ-20261022-004", "recipient@example.com", "Product", "2026-10-01", "2026-10-22", workbook, 1, "success")
    with pytest.raises(mailer.RequestDeliveryError):
        mailer.send_request_report(*args)
    marker = json.loads((request_dir / ".email_delivery.json").read_text())
    assert marker["status"] == "delivery_uncertain" and "recipient@example.com" not in str(marker)
    with pytest.raises(RuntimeError, match="already claimed"):
        mailer.send_request_report(*args)


def test_request_email_retries_a_pre_acceptance_network_failure(tmp_path, monkeypatch):
    import Auto_Email_Flipkart_OnlyJk as mailer
    monkeypatch.setattr(mailer, "__file__", str(tmp_path / "mailer.py"))
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "app-password-placeholder")
    monkeypatch.setattr(mailer.time, "sleep", lambda _seconds: None)
    calls = {"count": 0, "sent": 0}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("temporary connection failure")
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def starttls(self): pass
        def login(self, user, password): pass
        def send_message(self, message): calls["sent"] += 1

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    request_dir = tmp_path / "reports" / "REQ-20261022-005"
    request_dir.mkdir(parents=True)
    workbook = request_dir / "Product.xlsx"
    workbook.write_bytes(b"workbook")
    record = mailer.send_request_report("REQ-20261022-005", "recipient@example.com", "Product",
                                        "2026-10-01", "2026-10-22", workbook, 1, "success")
    assert calls == {"count": 2, "sent": 1} and record["status"] == "sent"
