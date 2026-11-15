"""KiCad IPC requests without a time limit, watched for KiCad going away.

A KiCad command takes as long as KiCad needs (a zone fill on a large board can take
minutes), so a request waits for its answer with no timeout (nng's infinite wait). The one
case where the answer can never come is KiCad exiting while it works; a watcher looks every
second for the KiCad serving the request (its IPC socket file) and, when it is gone, closes
the client's socket so the request fails with that cause instead of waiting forever.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Any

#: nng's "wait forever" for send and receive.
NO_LIMIT_MS = -1
#: How often the watcher looks for KiCad (a poll, not a limit).
LOOK_EVERY_S = 1.0


def _socket_file(socket_path: str | None) -> Path | None:
    if not socket_path:
        from ..instance.manager import default_socket  # noqa: PLC0415

        socket_path = default_socket()
    if socket_path.startswith("ipc://"):
        return Path(socket_path.removeprefix("ipc://"))
    return None


def watch(client: Any, socket_path: str | None) -> Any:  # noqa: ANN401 - kipy's KiCad
    """Wrap the client's requests so KiCad exiting ends a waiting request."""
    inner = getattr(client, "_client", None)
    send = getattr(inner, "send", None)
    if inner is None or not callable(send):
        return client
    socket_file = _socket_file(socket_path)

    def watched(command: Any, response_type: Any) -> Any:  # noqa: ANN401
        done = threading.Event()

        def look() -> None:
            while not done.wait(LOOK_EVERY_S):
                if socket_file is not None and not socket_file.exists():
                    with contextlib.suppress(Exception):
                        inner._conn.close()  # the waiting request fails: KiCad is gone
                    return

        threading.Thread(target=look, daemon=True, name="kicad-ipc-watch").start()
        try:
            return send(command, response_type)
        finally:
            done.set()

    inner.send = watched
    return client
