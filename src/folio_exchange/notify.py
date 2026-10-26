"""Alerts (RUN-2): a chat webhook (Slack-style ``{"text"}``, or a Teams Workflows Adaptive Card), and email.

Alerting never raises: a broken alert channel is logged, and the run's own status
stands. Neither the webhook URL nor any password is ever logged or included in a
message.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

import requests

from . import credentials

log = logging.getLogger(__name__)

QUIET = {"success", "built", "preview"}


def should_alert(alerts: dict, status: str) -> bool:
    on = (alerts or {}).get("on", "failure")
    if on == "never":
        return False
    if on == "always":
        return True
    return status not in QUIET


def _webhook(alerts: dict, account: str, text: str) -> bool:
    url = credentials.resolve(alerts.get("webhook"), credentials.WEBHOOK_SERVICE, account)
    if not url:
        log.warning("Alert webhook is configured but its URL was not found (%s)",
                    credentials.source(alerts.get("webhook"), credentials.WEBHOOK_SERVICE, account))
        return False
    if alerts.get("webhook_format") == "teams":
        # Teams Workflows ("When a Teams webhook request is received") take an Adaptive Card.
        lines = text.split("\n", 1)
        body = [{"type": "TextBlock", "text": lines[0], "weight": "Bolder", "wrap": True}]
        if len(lines) > 1:
            body.append({"type": "TextBlock", "text": lines[1], "wrap": True})
        payload = {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                        "version": "1.4", "body": body},
        }]}
    else:
        payload = {"text": text}
    try:
        resp = requests.post(url, json=payload, timeout=30)
    except requests.RequestException as exc:
        log.warning("Alert webhook failed: %s", type(exc).__name__)
        return False
    if not resp.ok:
        log.warning("Alert webhook returned HTTP %s", resp.status_code)
        return False
    return True


def _email(email: dict, subject: str, text: str) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email["from"]
    msg["To"] = ", ".join(email["to"])
    msg.set_content(text)
    user = email.get("username")
    password = None
    if user:
        account = credentials.sftp_account(user, email["smtp_host"])
        password = credentials.resolve(email.get("secret"), credentials.SMTP_SERVICE, account)
        if not password:
            log.warning("Alert email: no SMTP password found (%s)",
                        credentials.source(email.get("secret"), credentials.SMTP_SERVICE, account))
            return False
    try:
        port = int(email.get("smtp_port") or 587)
        ctx = ssl.create_default_context()
        smtp_cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        kwargs = {"context": ctx} if port == 465 else {}
        with smtp_cls(email["smtp_host"], port, timeout=60, **kwargs) as smtp:
            if port != 465 and email.get("starttls", True):
                smtp.starttls(context=ctx)
            elif port != 465 and user:
                log.warning("Alert email: refusing to log in without STARTTLS")
                return False
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("Alert email failed: %s", type(exc).__name__)
        return False
    return True


def send(alerts: dict, subject: str, text: str, *, account: str = "default") -> list[str]:
    """Send ``text`` on every configured channel. Returns the channels that worked; never raises.

    ``account`` is the keyring account for the webhook URL (the site folder's name)."""
    sent: list[str] = []
    alerts = alerts or {}
    try:
        if alerts.get("webhook") and _webhook(alerts, account, f"{subject}\n{text}"):
            sent.append("webhook")
        if alerts.get("email") and _email(alerts["email"], subject, text):
            sent.append("email")
    except Exception as exc:  # noqa: BLE001 - alerting must never break a run
        log.warning("Alert failed: %s", type(exc).__name__)
    return sent
