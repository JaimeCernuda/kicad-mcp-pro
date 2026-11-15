"""Stop a started KiCad when the server that started it is gone, however it went.

``python -m kicad_mcp.instance.watch <pgid>`` runs in its own session and blocks on its
standard input, a pipe only the server holds. The pipe closes when the server exits, is
terminated, is killed or crashes; the watcher then stops the KiCad process group (SIGTERM,
then SIGKILL) and exits. ``atexit`` alone missed every exit but a normal one.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def main(argv: list[str]) -> int:
    pgid = int(argv[1])
    with contextlib.suppress(OSError):
        while sys.stdin.buffer.read(4096):
            pass
    if not _alive(pgid):
        return 0
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while _alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.2)
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
