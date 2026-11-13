"""What alerts send (improvement plan 4.1, TEST-5): the Slack and Teams payloads, the webhook's answers, and email
over TLS. Nothing reaches the network: requests.post and the SMTP classes are replaced."""
import logging

import pytest
import requests

from folio_exchange import notify

HOOK = "https://hooks.example.org/services/T000/B000/SECRETPART"     # gitleaks:allow - made up
MAIL_PASSWORD = "mail-password-123"                                    # gitleaks:allow - made up
SUBJECT = "FOLIO-Exchange [site] chq: FAILED"
TEXT = "Feed chq (collectionHQ), recipe chq-academic@1.0.0: failed.\nDelivery failed (TimeoutError).\nRun folder: out/chq/1"


class Answer:
    def __init__(self, status: int = 200):
        self.status_code = status
        self.ok = 200 <= status < 400


class Posts(list):
    """The webhook posts made, as (url, json, kwargs); ``post`` is the stand-in for requests.post."""


@pytest.fixture
def posted(monkeypatch):
    """Every webhook post; answers 200 unless ``posted.post.answer`` says otherwise."""
    calls = Posts()

    def post(url, json=None, **kwargs):
        calls.append((url, json, kwargs))
        if isinstance(post.answer, Exception):
            raise post.answer
        return Answer(post.answer)

    post.answer = 200
    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setenv("HOOK", HOOK)
    calls.post = post
    return calls


def slack(**more) -> dict:
    return {"on": "failure", "webhook": "env:HOOK", "webhook_format": "slack", **more}


# ------------------------------------------------------------------ webhook payloads
def test_slack_gets_the_subject_and_text_as_one_text_field(posted):
    assert notify.send(slack(), SUBJECT, TEXT, account="site") == ["webhook"]
    [(url, payload, kwargs)] = posted
    assert url == HOOK
    assert payload == {"text": f"{SUBJECT}\n{TEXT}"}
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == 30


def test_teams_gets_an_adaptive_card_with_the_subject_in_bold(posted):
    assert notify.send(slack(webhook_format="teams"), SUBJECT, TEXT, account="site") == ["webhook"]
    [(_url, payload, _kwargs)] = posted
    assert payload["type"] == "message"
    [attachment] = payload["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    first, rest = card["body"]
    assert first == {"type": "TextBlock", "text": SUBJECT, "weight": "Bolder", "wrap": True}
    assert rest == {"type": "TextBlock", "text": TEXT, "wrap": True}


def test_a_one_line_teams_card_has_one_block(posted):
    notify.send(slack(webhook_format="teams"), SUBJECT, "", account="site")
    [(_url, payload, _kwargs)] = posted
    body = payload["attachments"][0]["content"]["body"]
    assert [b["text"] for b in body] == [SUBJECT, ""]          # subject, then the (empty) text after its line break
    notify._webhook(slack(webhook_format="teams"), "site", "one line only")
    assert [b["text"] for b in posted[-1][1]["attachments"][0]["content"]["body"]] == ["one line only"]


# ------------------------------------------------------------------ webhook answers
@pytest.mark.parametrize("answer,words", [
    (301, "redirect (HTTP 301)"), (308, "redirect (HTTP 308)"), (403, "HTTP 403"), (500, "HTTP 500"),
])
def test_a_redirect_or_an_error_answer_is_a_failure_that_names_no_url(posted, caplog, answer, words):
    posted.post.answer = answer
    with caplog.at_level(logging.WARNING, logger="folio_exchange.notify"):
        assert notify.send(slack(), SUBJECT, TEXT, account="site") == []
    assert words in caplog.text
    assert "hooks.example.org" not in caplog.text and "SECRETPART" not in caplog.text


def test_a_network_error_is_a_failure_that_names_no_url(posted, caplog):
    posted.post.answer = requests.ConnectionError(f"Max retries exceeded with url: {HOOK}")
    with caplog.at_level(logging.WARNING, logger="folio_exchange.notify"):
        assert notify.send(slack(), SUBJECT, TEXT, account="site") == []
    assert "ConnectionError" in caplog.text and "SECRETPART" not in caplog.text


@pytest.mark.parametrize("url", ["http://hooks.example.org/x", "ftp://hooks.example.org/x", "not a url"])
def test_a_refused_url_is_never_posted_to(posted, monkeypatch, caplog, url):
    monkeypatch.setenv("HOOK", url)
    with caplog.at_level(logging.WARNING, logger="folio_exchange.notify"):
        assert notify.send(slack(), SUBJECT, TEXT, account="site") == []
    assert posted == [] and "refused" in caplog.text and "hooks.example.org" not in caplog.text


def test_plain_http_is_allowed_only_to_this_computer(posted, monkeypatch):
    monkeypatch.setenv("HOOK", "http://127.0.0.1:9/hook")
    assert notify.send(slack(), SUBJECT, TEXT, account="site") == ["webhook"]


def test_a_missing_webhook_url_is_said_without_sending(posted, monkeypatch, caplog):
    monkeypatch.delenv("HOOK")
    with caplog.at_level(logging.WARNING, logger="folio_exchange.notify"):
        assert notify.send(slack(), SUBJECT, TEXT, account="site") == []
    assert posted == [] and "environment variable HOOK is not set" in caplog.text


@pytest.mark.parametrize("on,status,sent", [
    ("failure", "failed", True), ("failure", "blocked", True), ("failure", "held", True),
    ("failure", "success", False), ("failure", "built", False), ("always", "success", True),
    ("never", "failed", False),
])
def test_when_alerts_go(on, status, sent):
    assert notify.should_alert({"on": on}, status) is sent


# ------------------------------------------------------------------ email
class FakeSMTP:
    """Stands in for smtplib.SMTP and SMTP_SSL, recording what was done in order."""
    made: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context, self.steps = host, port, context, []
        FakeSMTP.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.steps.append("quit")

    def starttls(self, context=None):
        assert context is not None
        self.steps.append("starttls")

    def login(self, user, password):
        self.steps.append(("login", user, password))

    def send_message(self, msg):
        self.steps.append(("send", msg["Subject"], msg["To"], msg.get_content().strip()))


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.made = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTPSSL)
    monkeypatch.setenv("SMTP_PW", MAIL_PASSWORD)
    return FakeSMTP.made


def mail(**more) -> dict:
    email = {"to": ["systems@library.example", "lead@library.example"], "from": "fx@library.example",
             "smtp_host": "smtp.library.example", "smtp_port": 587, "starttls": True, "username": "fx",
             "secret": "env:SMTP_PW", **more}
    return {"on": "failure", "email": email}


def test_email_starts_tls_before_logging_in(smtp):
    assert notify.send(mail(), SUBJECT, TEXT) == ["email"]
    [conn] = smtp
    assert type(conn) is FakeSMTP and (conn.host, conn.port) == ("smtp.library.example", 587)
    assert conn.steps[0] == "starttls"
    assert conn.steps[1] == ("login", "fx", MAIL_PASSWORD)
    assert conn.steps[2] == ("send", SUBJECT, "systems@library.example, lead@library.example", TEXT)


def test_port_465_uses_smtp_over_tls_with_a_checked_certificate(smtp):
    assert notify.send(mail(smtp_port=465, starttls=False), SUBJECT, TEXT) == ["email"]
    [conn] = smtp
    assert type(conn) is FakeSMTPSSL and conn.context is not None
    assert conn.context.verify_mode.name == "CERT_REQUIRED" and conn.context.check_hostname
    assert "starttls" not in conn.steps and conn.steps[0] == ("login", "fx", MAIL_PASSWORD)


def test_no_login_without_tls(smtp, caplog):
    with caplog.at_level(logging.WARNING, logger="folio_exchange.notify"):
        assert notify.send(mail(starttls=False), SUBJECT, TEXT) == []
    [conn] = smtp
    assert not any(isinstance(s, tuple) and s[0] in ("login", "send") for s in conn.steps)
    assert "refusing to log in without STARTTLS" in caplog.text


def test_a_relay_without_a_login_may_send_without_tls(smtp):
    assert notify.send(mail(starttls=False, username=None), SUBJECT, TEXT) == ["email"]
    [conn] = smtp
    assert [s[0] if isinstance(s, tuple) else s for s in conn.steps] == ["send", "quit"]


def test_a_missing_mail_password_connects_to_nothing(smtp, monkeypatch, caplog):
    monkeypatch.delenv("SMTP_PW")
    with caplog.at_level(logging.WARNING, logger="folio_exchange.notify"):
        assert notify.send(mail(), SUBJECT, TEXT) == []
    assert smtp == [] and "no SMTP password found" in caplog.text


def test_a_mail_failure_names_its_type_and_never_the_password(smtp, monkeypatch, caplog):
    def refuse(self, user, password):
        raise notify.smtplib.SMTPAuthenticationError(535, f"bad password {password}".encode())

    monkeypatch.setattr(FakeSMTP, "login", refuse)
    with caplog.at_level(logging.WARNING, logger="folio_exchange.notify"):
        assert notify.send(mail(), SUBJECT, TEXT) == []
    assert "SMTPAuthenticationError" in caplog.text and MAIL_PASSWORD not in caplog.text


# ------------------------------------------------------------------ both channels
def test_a_broken_webhook_does_not_stop_the_email(posted, smtp, monkeypatch, caplog):
    def broken(*_a, **_k):
        raise RuntimeError("the keyring backend failed")

    monkeypatch.setattr(notify, "_webhook", broken)
    alerts = {**slack(), "email": mail()["email"]}
    with caplog.at_level(logging.WARNING, logger="folio_exchange.notify"):
        assert notify.send(alerts, SUBJECT, TEXT) == ["email"]
    assert "RuntimeError" in caplog.text


def test_a_broken_email_does_not_undo_the_webhook(posted, smtp, monkeypatch):
    def broken(*_a, **_k):
        raise KeyError("to")

    monkeypatch.setattr(notify, "_email", broken)
    alerts = {**slack(), "email": mail()["email"]}
    assert notify.send(alerts, SUBJECT, TEXT) == ["webhook"]
