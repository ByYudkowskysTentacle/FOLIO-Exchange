"""One window per site: a second start for the same site asks the first to come to the front, then exits.

The first start listens on a per-user local socket (a named pipe on Windows) named after this Windows user
and the site folder (``places.instance_name``); only this user can connect. A second start connects and is
told the first one's process id. On Windows, where pipe names are shared between users, it checks that the
pipe's server is that process and runs as this user. Pipe names are also shared between the sessions of one
user (a second sign-in, through Remote Desktop say), so a window in another session is reported, not shown
where nobody sees it. Then it lets that process take the foreground (Windows gives focus only to the program
the person started last, which is the second one), sends ``show`` and waits briefly for ``ok``. The first
accepts nothing else: any other message, or more than a line, is dropped.

Windows lets a second server listen on a pipe name that is already taken, so there the name is also held by a
named mutex, which Windows removes with the process that made it. On macOS and Linux Qt puts a socket server that
only this user may reach in place by renaming it over whatever has the name, a live socket included, so there
the name is held by an exclusive lock (``flock``) on a file in this user's settings folder, which the system lets
go with the process, and is taken only when nothing answers on it; a socket file that does not answer is one a
crash left behind.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket

from . import places

log = logging.getLogger(__name__)

HELLO = b"fx "                       # the first start's greeting: b"fx <pid>\n"
SHOW, OK = b"show\n", b"ok\n"
LINE_MOST = 64                       # longer than any message, so a long one is dropped unread
ASK_MS = 2000                        # how long a second start waits for each answer
CONNECTION_MS = 3000                 # how long the first start keeps a connection that says nothing
STEP_MS = 20                         # each slice of a blocking wait (see _wait)
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_USER = 1                       # TOKEN_INFORMATION_CLASS: the user the process runs as
HOLDS = "instances"                  # macOS and Linux: the folder of lock files, in places.config_dir()


class OpenElsewhere(Exception):
    """The site's window is open in another Windows session of this user, where this one cannot show it."""


class InstanceServer(QObject):
    """The first start's side. ``shown`` fires when a second start asks for the window."""

    shown = Signal()

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.server: QLocalServer | None = None
        self.guard: int | None = None

    def listen(self) -> bool:
        got = self._listen(self.name)
        if got is None:
            return False
        self.server, self.guard = got
        return True

    def rename(self, name: str) -> bool:
        """Listen under a new name (the site folder was renamed); keeps the old name if the new one is taken."""
        if name == self.name:
            return True
        got = self._listen(name)
        if got is None:
            return False
        self.close()
        self.name, (self.server, self.guard) = name, got
        return True

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server.setParent(None)        # its deletion pending must not outlive this object as a child
            self.server.deleteLater()
            self.server = None
        _let_go(self.guard)
        self.guard = None

    def _listen(self, name: str) -> tuple[QLocalServer, int | None] | None:
        guard = _hold(name)
        if guard is False:
            return None
        if sys.platform != "win32" and _answers(name):
            _let_go(guard)                     # never take a name a window answers on, even with the lock: one
            return None                        # with no lock, or another settings folder's, may hold it
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(lambda: self._accept(server))
        if not _listen(server, name):
            server.deleteLater()
            _let_go(guard)
            return None
        return server, guard

    def _accept(self, server: QLocalServer) -> None:
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            sock.setReadBufferSize(LINE_MOST)
            QTimer.singleShot(CONNECTION_MS, sock, sock.abort)
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(sock.deleteLater)
            sock.write(HELLO + str(os.getpid()).encode() + b"\n")

    def _read(self, sock: QLocalSocket) -> None:
        if not sock.canReadLine():
            if sock.bytesAvailable() >= LINE_MOST:
                sock.abort()
            return
        line = bytes(sock.readLine(LINE_MOST).data())
        if line == SHOW:
            self.shown.emit()
            sock.write(OK)
            sock.flush()
        sock.disconnectFromServer()


def _hold(name: str) -> int | None | bool:
    """A hold on ``name`` that the system lets go with the process, or False when another start has it.

    Windows: a handle to a named mutex. Elsewhere: an open file (its descriptor) in this user's settings folder
    with an exclusive ``flock`` on it; a second open of the file, in this process or another, cannot lock it,
    and the descriptor is not inherited by programs this one starts. None when no hold can be made there (a
    settings folder that cannot be written, say). Either way the name is taken there only when nothing answers
    on it, since a window that had no hold, or one started with another settings folder (another
    ``XDG_CONFIG_HOME``), may have it."""
    if sys.platform != "win32":
        return _lock_file(name)
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        log.debug("CreateMutexW failed: %s", ctypes.get_last_error())
        return False
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _let_go(handle)
        return False
    return handle


def lock_path(name: str) -> Path:
    """macOS and Linux: the file whose lock holds ``name``. It stays when let go: removing it could let two
    starts lock two different files of the same name."""
    return places.config_dir() / HOLDS / f"{name}.lock"


def _lock_file(name: str) -> int | bool | None:
    import fcntl

    path = lock_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        log.warning("Could not make the lock file %s (%s); the window's name is taken only when free", path, exc)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if isinstance(exc, BlockingIOError):
            return False
        log.warning("Could not lock %s (%s); the window's name is taken only when free", path, exc)
        return None
    return fd


def _let_go(handle: int | None) -> None:
    if handle is None or handle is False:
        return
    if sys.platform != "win32":
        os.close(handle)                       # lets the flock go
        return
    if handle:
        import ctypes

        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))


def _listen(server: QLocalServer, name: str) -> bool:
    """Listen on ``name``, whose hold (``_hold``) this start has. A name left behind by a crashed start (a socket
    file on macOS and Linux; Windows removes a named pipe with its process) is replaced: Qt renames the new
    socket over it, or, should it report the name in use, it is removed once a connection to it has failed."""
    if server.listen(name):
        return True
    if server.serverError() == QAbstractSocket.SocketError.AddressInUseError and not _answers(name):
        QLocalServer.removeServer(name)
        return server.listen(name)
    log.debug("Could not listen on %s: %s", name, server.errorString())
    return False


def _answers(name: str) -> bool:
    """Whether something is listening on ``name`` (a quick connect, nothing sent)."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    ok = _connected(sock, time.monotonic() + 0.5)
    sock.abort()
    return ok


def _let_take_focus(pid: int) -> None:
    """Windows: allow process ``pid`` to bring its window to the front (this process is in the foreground)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(int(pid))
    except (OSError, AttributeError, ValueError) as exc:
        log.debug("AllowSetForegroundWindow failed: %s", exc)


def _trusted_pid(sock: QLocalSocket, said: int) -> int | None:
    """The process id of the window at the other end of ``sock``, or None when it is not to be trusted.

    Windows shares pipe names between users and sessions, and anyone who knows a person's account name and site
    folder can work out the name, so another account could take it first. There the pipe's server must run as
    this Windows user and be the process it says it is; only then is it let take the foreground. Elsewhere (the
    app is made for Windows) the greeting is taken as it is."""
    if sys.platform != "win32":
        return said
    try:
        pid = _pipe_server_pid(int(sock.socketDescriptor()))
        if pid != said or _user_sid(pid) != _user_sid(os.getpid()):
            return None
    except OSError as exc:                     # another user's process cannot be read, which says enough
        log.debug("Could not tell whose the pipe's server is: %s", exc)
        return None
    return pid


def _same_session(pid: int) -> bool:
    """Whether process ``pid`` runs in this Windows session (always, elsewhere). When it cannot be told, the
    process is taken to be here, as before sessions were compared."""
    if sys.platform != "win32":
        return True
    try:
        return _session_id(pid) == _session_id(os.getpid())
    except OSError as exc:
        log.debug("Could not tell which Windows session process %s is in: %s", pid, exc)
        return True


def _session_id(pid: int) -> int:
    """Windows: the session (sign-in) process ``pid`` runs in."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    return session.value


def _pipe_server_pid(handle: int) -> int:
    """Windows: the process id of the server at the other end of a named pipe's client ``handle``."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pid.value


def _user_sid(pid: int) -> str:
    """Windows: the user process ``pid`` runs as, as a SID in text. Raises OSError when it cannot be read."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]

    def check(ok) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    check(process)
    try:
        token = wintypes.HANDLE()
        check(advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)))
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))      # asks the size
            buf = ctypes.create_string_buffer(size.value)
            check(advapi32.GetTokenInformation(token, TOKEN_USER, buf, size, ctypes.byref(size)))
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]     # TOKEN_USER starts with the SID
            text = wintypes.LPWSTR()
            check(advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)))
            try:
                return text.value
            finally:
                kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def ask_to_show(name: str, wait_ms: int | None = None) -> bool:
    """A second start's side: ask the window already open under ``name`` to come to the front. True when it
    answered; False when nothing listens there, it is not this user's (see ``_trusted_pid``), or it did not
    answer in time (``wait_ms``, else ASK_MS, for each answer). Raises OpenElsewhere when it is this user's
    window in another Windows session, which is not asked to show itself there. It waits without an event loop
    running (before the app's own window exists), handling this thread's events between short waits."""
    wait_ms = ASK_MS if wait_ms is None else wait_ms
    sock = QLocalSocket()
    deadline = time.monotonic() + wait_ms / 1000
    sock.connectToServer(name)
    try:
        if not _connected(sock, deadline):
            return False
        hello = _line(sock, deadline)
        if not hello.startswith(HELLO):
            return False
        try:
            said = int(hello[len(HELLO):].strip())
        except ValueError:
            return False
        pid = _trusted_pid(sock, said)
        if pid is None:
            log.warning("A program that is not this user's FOLIO-Exchange holds the name %s; it was not asked to "
                        "show anything", name)
            return False
        if not _same_session(pid):
            raise OpenElsewhere(name)
        _let_take_focus(pid)
        deadline = time.monotonic() + wait_ms / 1000
        sock.write(SHOW)
        if not _wait(lambda ms: sock.waitForBytesWritten(ms) or not sock.bytesToWrite(), deadline):
            return False
        return _line(sock, deadline) == OK
    finally:
        sock.abort()


def _wait(step, deadline: float) -> bool:
    """Call ``step(ms)`` (a Qt waitFor...) in short slices until it is true or the deadline passes, handling
    this thread's events in between, so a window open in this same process (in tests) can answer."""
    app = QCoreApplication.instance()
    here = app is not None and app.thread() is QThread.currentThread()
    while True:
        left = int((deadline - time.monotonic()) * 1000)
        if left <= 0:
            return False
        if step(min(STEP_MS, left)):
            return True
        if here:
            app.processEvents()


def _connected(sock: QLocalSocket, deadline: float) -> bool:
    def done(ms: int) -> bool:
        return sock.waitForConnected(ms) or sock.state() == QLocalSocket.LocalSocketState.UnconnectedState

    return _wait(done, deadline) and sock.state() == QLocalSocket.LocalSocketState.ConnectedState


def _line(sock: QLocalSocket, deadline: float) -> bytes:
    def ready(ms: int) -> bool:
        sock.waitForReadyRead(ms)
        return (sock.canReadLine() or sock.bytesAvailable() >= LINE_MOST
                or sock.state() == QLocalSocket.LocalSocketState.UnconnectedState)

    if not _wait(ready, deadline) or not sock.canReadLine():
        return b""
    return bytes(sock.readLine(LINE_MOST).data())


def claim(name: str) -> tuple[str, InstanceServer | None]:
    """Become the window for ``name``, or hand over to the one already open. Returns ("first", server) when
    this start should open the window, ("shown", None) when the open window was asked to come to the front,
    ("elsewhere", None) when it is open in another Windows session of this user, or ("busy", None) when
    something holds the name but does not answer."""
    try:
        if ask_to_show(name):
            return "shown", None
        server = InstanceServer(name)
        if server.listen():
            return "first", server
        if ask_to_show(name):                  # another start won the race just now
            return "shown", None
    except OpenElsewhere:
        return "elsewhere", None
    return "busy", None
