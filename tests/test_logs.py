"""The site's log is rotated when a program starts, and kept open while the program runs."""
import logging

import pytest

from folio_exchange import logsetup
from folio_exchange.logsetup import rotate, setup_logging


@pytest.fixture
def clean_logging():
    """Put the root logger back as it was: setup_logging replaces FOLIO-Exchange's own handlers there."""
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for h in [h for h in root.handlers if h not in before]:
        root.removeHandler(h)
        h.close()
    root.setLevel(level)


def test_a_small_log_is_left_alone(tmp_path):
    log = tmp_path / "folio-exchange.log"
    log.write_text("x" * 10)
    assert rotate(log, max_bytes=100) is False
    assert [p.name for p in tmp_path.iterdir()] == ["folio-exchange.log"]


def test_a_large_log_moves_to_dot_1_and_the_oldest_is_dropped(tmp_path):
    log = tmp_path / "folio-exchange.log"
    for n in range(1, 4):
        (tmp_path / f"folio-exchange.log.{n}").write_text(f"old {n}")
    log.write_text("x" * 200)
    assert rotate(log, max_bytes=100, keep=3) is True
    assert not log.exists()
    assert (tmp_path / "folio-exchange.log.1").read_text() == "x" * 200
    assert (tmp_path / "folio-exchange.log.2").read_text() == "old 1"
    assert (tmp_path / "folio-exchange.log.3").read_text() == "old 2"          # "old 3" was the oldest kept
    assert not (tmp_path / "folio-exchange.log.4").exists()


def test_a_missing_log_or_one_that_cannot_be_moved_is_not_an_error(tmp_path, monkeypatch):
    assert rotate(tmp_path / "none.log", max_bytes=1) is False
    log = tmp_path / "folio-exchange.log"
    log.write_text("x" * 200)

    def in_use(*_a):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(logsetup.os, "replace", in_use)                        # Windows, with the file open elsewhere
    assert rotate(log, max_bytes=100) is False
    assert log.read_text() == "x" * 200


def test_setup_rotates_the_site_log_and_the_cron_log(tmp_path, monkeypatch, clean_logging):
    monkeypatch.setattr(logsetup, "LOG_MAX_BYTES", 100)
    (tmp_path / "folio-exchange.log").write_text("a" * 200)
    (tmp_path / "cron.log").write_text("b" * 200)
    setup_logging(tmp_path, False, console=False)
    logging.getLogger("folio_exchange.test").info("first line after the rotation")
    assert (tmp_path / "folio-exchange.log.1").read_text() == "a" * 200
    assert (tmp_path / "cron.log.1").read_text() == "b" * 200
    assert "first line after the rotation" in (tmp_path / "folio-exchange.log").read_text(encoding="utf-8")


def test_the_log_stays_open_while_the_program_runs(tmp_path, clean_logging):
    """Regression (review of 12 Nov): a handler that opened the file for each record could raise into a delivery
    when the file couldn't be opened, and let another program rename the site folder under a running one. A
    plain FileHandler keeps the file open, so neither can happen."""
    setup_logging(tmp_path, False, console=False)
    [handler] = [h for h in logging.getLogger().handlers if getattr(h, "_folio_exchange", False)]
    assert type(handler) is logging.FileHandler and handler.stream is not None
    logging.getLogger("folio_exchange.test").info("one")
    assert handler.stream is not None and not handler.stream.closed


def test_the_log_exists_from_setup_as_before(tmp_path, clean_logging):
    """`secret set` (which logs nothing) and places.unfinished_site rely on the file existing from setup."""
    setup_logging(tmp_path, False, console=False)
    assert (tmp_path / "folio-exchange.log").is_file()
