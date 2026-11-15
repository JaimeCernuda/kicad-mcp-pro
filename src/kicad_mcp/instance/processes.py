"""Process checks the instance lifetime needs, on Linux, macOS and Windows (standard library).

A started KiCad runs in its own process group (POSIX) or process tree (Windows); stopping it
stops the whole group or tree, and nothing is stopped unless its command line names KiCad.
"""

from __future__ import annotations

import contextlib
import os
import platform
import signal
import subprocess  # nosec B404 - argv-only ps/tasklist/taskkill, shell=False
import time
from pathlib import Path

WINDOWS = platform.system() == "Windows"


def command_line(pid: int) -> str:
    """The process's command line, or ``""`` when it is gone or cannot be read."""
    if not WINDOWS:
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.exists():
            with contextlib.suppress(OSError):
                return proc.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            return ""
        argv = ["ps", "-p", str(pid), "-o", "command="]
    else:
        argv = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    try:
        # No time limit; plain subprocess (not run_to_end): this runs inside the stop path,
        # where a pending cancellation must not interrupt the stop.
        result = subprocess.run(  # noqa: S603, S607  # nosec B603 B607 - system ps / tasklist
            argv, capture_output=True, text=True, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = result.stdout.strip()
    return "" if WINDOWS and not text.startswith('"') else text


def is_kicad(pid: int) -> bool:
    """Whether ``pid`` is a running KiCad (its command line names it), never a reused pid."""
    text = command_line(pid).casefold()
    return "kicad" in text or "pcbnew" in text


def alive(pid: int) -> bool:
    """Whether the group (POSIX) or process (Windows) led by ``pid`` still runs."""
    if WINDOWS:
        return bool(command_line(pid))
    try:
        os.killpg(pid, 0)
    except OSError:
        return False
    return True


def stop_tree(pid: int, wait_s: float = 10.0) -> None:
    """Stop the group or tree led by ``pid``: politely, then by force."""
    if WINDOWS:
        for force in ([], ["/F"]):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(  # noqa: S603  # nosec B603 B607 - system taskkill
                    ["taskkill", "/PID", str(pid), "/T", *force],  # noqa: S607
                    capture_output=True,
                    check=False,
                )
            # The stop's own grace before forcing: not a limit on any operation.
            deadline = time.monotonic() + wait_s
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            if not alive(pid):
                return
        return
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGTERM)
    # The stop's own grace before SIGKILL: not a limit on any operation.
    deadline = time.monotonic() + wait_s
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)
