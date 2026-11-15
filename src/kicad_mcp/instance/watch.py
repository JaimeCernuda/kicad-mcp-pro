"""Stop a started KiCad when the server that started it is gone, however it went.

``python -m kicad_mcp.instance.watch <pid>`` runs in its own session (or process group on
Windows) and blocks on its standard input, a pipe only the server holds. The pipe closes when
the server exits, is terminated, is killed or crashes; the watcher then stops KiCad's group or
tree (politely, then by force) and exits. ``atexit`` alone missed every exit but a normal one.
"""

from __future__ import annotations

import contextlib
import sys

from kicad_mcp.instance import processes


def main(argv: list[str]) -> int:
    pid = int(argv[1])
    with contextlib.suppress(OSError):
        while sys.stdin.buffer.read(4096):
            pass
    if processes.alive(pid) and processes.is_kicad(pid):
        processes.stop_tree(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
