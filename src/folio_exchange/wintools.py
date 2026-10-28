"""Windows' own command-line tools (schtasks, icacls), started by their full path.

Started by a bare name, Windows looks for the program in the current folder before System32, and the app or a
command may be started from a shared folder that others can write to; a schtasks.exe planted there would run
as this user. So every call goes through ``system_tool``.
"""
from __future__ import annotations

import os
import sys


def system_folder() -> str:
    """Windows' System32 folder, as Windows itself reports it (SystemRoot\\System32 if it cannot be asked)."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW(buf, len(buf))
        if 0 < size < len(buf):
            return buf.value
    except (OSError, AttributeError):
        pass
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")


def system_tool(name: str) -> str:
    """The full path of Windows' own ``name`` (for example "schtasks") in System32. Elsewhere the bare name,
    since these tools exist only on Windows."""
    if sys.platform != "win32":
        return name
    return os.path.join(system_folder(), f"{name}.exe")
