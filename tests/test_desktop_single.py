"""One window per site: the hand-off between two starts, a name left by a crash, and a window that hangs.

Both starts live in this one process: the second one's waits handle this thread's events, so the first answers."""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

from PySide6.QtCore import QDir  # noqa: E402
from PySide6.QtNetwork import QLocalSocket  # noqa: E402

from folio_exchange.desktop import single  # noqa: E402


@pytest.fixture
def name():
    return f"fx-test-{uuid.uuid4().hex[:16]}"           # each test its own name, so runs never meet


def test_a_second_start_brings_the_first_to_the_front(qtbot, name):
    first = single.InstanceServer(name)
    assert first.listen()
    shown = []
    first.shown.connect(lambda: shown.append(1))
    assert single.ask_to_show(name) is True
    assert shown == [1]
    assert single.claim(name) == ("shown", None)
    assert shown == [1, 1]
    assert not single.InstanceServer(name).listen()         # the name stays the first one's
    first.close()


def test_the_first_start_takes_the_name_and_gives_it_up_on_close(qtbot, name):
    kind, server = single.claim(name)
    assert kind == "first" and server is not None
    server.close()
    again = single.InstanceServer(name)
    assert again.listen()
    again.close()


def test_only_show_is_accepted(qtbot, name):
    first = single.InstanceServer(name)
    assert first.listen()
    shown = []
    first.shown.connect(lambda: shown.append(1))

    def other():
        sock = QLocalSocket()
        deadline = time.monotonic() + 3
        sock.connectToServer(name)
        single._connected(sock, deadline)
        hello = single._line(sock, deadline)
        sock.write(b"open a file\n")
        single._wait(lambda ms: sock.waitForBytesWritten(ms) or not sock.bytesToWrite(), deadline)
        answer = single._line(sock, time.monotonic() + 1)
        sock.abort()
        return hello, answer

    hello, answer = other()
    assert hello.startswith(single.HELLO) and answer == b""      # greeted, then dropped without an answer
    assert shown == []
    first.close()


def test_a_window_that_does_not_answer_is_reported_not_doubled(qtbot, name, monkeypatch):
    hung = single.InstanceServer(name)
    monkeypatch.setattr(hung, "_accept", lambda server: None)      # takes connections, never answers
    assert hung.listen()
    monkeypatch.setattr(single, "ASK_MS", 300)
    assert single.ask_to_show(name, wait_ms=300) is False
    start = time.monotonic()
    kind, server = single.claim(name)
    assert (kind, server) == ("busy", None)
    assert time.monotonic() - start < 1.8          # regression: ASK_MS is read when asked, not fixed at import
    hung.close()


def test_a_server_that_is_not_this_users_is_not_trusted(qtbot, name, monkeypatch):
    """Regression: another Windows account could take a site's pipe name first and answer as the window would;
    the second start then let that process take the foreground, and exited as if its own window were shown."""
    squatter = single.InstanceServer(name)
    assert squatter.listen()
    shown, focus = [], []
    squatter.shown.connect(lambda: shown.append(1))
    monkeypatch.setattr(single, "_trusted_pid", lambda sock, said: None)      # as for another account's pipe
    monkeypatch.setattr(single, "_let_take_focus", focus.append)
    assert single.ask_to_show(name, wait_ms=300) is False
    assert shown == [] and focus == []
    squatter.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the pipe's owner is checked on Windows")
def test_the_pipes_server_must_be_the_process_it_says_it_is(qtbot, name):
    first = single.InstanceServer(name)
    assert first.listen()
    sock = QLocalSocket()
    sock.connectToServer(name)
    assert single._connected(sock, time.monotonic() + 3)
    assert single._trusted_pid(sock, os.getpid()) == os.getpid()            # this user's, and this process
    assert single._trusted_pid(sock, os.getpid() + 4) is None               # says it is another process
    sock.abort()
    first.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows removes a named pipe with the process that made it")
def test_a_name_left_by_a_crash_is_taken_over(qtbot, name):
    import socket

    path = QDir(QDir.tempPath()).filePath(name)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(path)                                    # a socket file nobody listens on, as after a crash
    stale.close()
    kind, server = single.claim(name)
    assert kind == "first"
    server.close()


def test_renaming_moves_to_the_new_name(qtbot, name):
    first = single.InstanceServer(name)
    assert first.listen()
    assert first.rename(name + "-new") and first.name == name + "-new"
    old = single.InstanceServer(name)
    assert old.listen()                                     # the old name is free again
    old.close()
    assert single.ask_to_show(name + "-new") is True
    first.close()


def test_a_window_in_another_windows_session_is_not_shown_there(qtbot, name, monkeypatch):
    """Regression: pipe names are shared between the sessions of one Windows user, so a start in a second
    session (Remote Desktop, say) raised the window in the first, where nobody saw it, and exited with nothing
    shown."""
    other = single.InstanceServer(name)
    assert other.listen()
    shown, focus = [], []
    other.shown.connect(lambda: shown.append(1))
    monkeypatch.setattr(single, "_same_session", lambda pid: False)
    monkeypatch.setattr(single, "_let_take_focus", focus.append)
    with pytest.raises(single.OpenElsewhere):
        single.ask_to_show(name, wait_ms=500)
    assert single.claim(name) == ("elsewhere", None)
    assert shown == [] and focus == []
    other.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sessions")
def test_this_process_is_in_its_own_session():
    assert single._session_id(os.getpid()) == single._session_id(os.getpid())
    assert single._same_session(os.getpid())
    assert single._same_session(2 ** 31) is True            # a process that cannot be read: as before


def test_a_closed_server_can_be_let_go_at_once(qtbot, name):
    """Regression: close() left the socket server's deletion pending as a child of the InstanceServer, and
    letting the InstanceServer go before that ran made the next window shown crash the process."""
    import gc

    from PySide6.QtWidgets import QWidget

    first = single.InstanceServer(name)
    assert first.listen()
    assert single.ask_to_show(name)
    first.close()
    del first
    gc.collect()
    w = QWidget()
    qtbot.addWidget(w)
    w.show()
    qtbot.wait(50)
    assert w.isVisible()
