"""HTTP requests that never give up on their own (see :mod:`.run_to_end`).

The request runs in a helper thread with no socket timeout: it waits for the server's
answer and body however long they take. The calling thread waits for it a tick at a time,
saying how long it has waited and stopping when the tool call is stopped. A connection the
other side refuses, resets or closes still fails with its cause; that is an answer, not a
time limit.
"""

from __future__ import annotations

import threading
import urllib.request
from collections.abc import Callable
from typing import IO, Any

from .run_to_end import POLL_S, Ticker

Opener = Callable[..., IO[bytes]]


def fetch(
    request: urllib.request.Request | str,
    *,
    what: str,
    opener: Opener | None = None,
    sink: Callable[[bytes], object] | None = None,
) -> bytes:
    """The response body of ``request`` (or, with ``sink``, each chunk passed to it).

    ``opener`` is called as ``opener(request)`` with no timeout; it defaults to
    :func:`urllib.request.urlopen`. HTTP errors and connection errors are raised as they are.
    """
    open_fn = opener or urllib.request.urlopen
    done: dict[str, Any] = {}
    state = {"read": 0}

    def work() -> None:
        try:
            chunks: list[bytes] = []
            with open_fn(request) as response:
                while True:
                    chunk = response.read(65536)
                    if not chunk:  # b"" (or "" from a text stream) is the end
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode()
                    state["read"] += len(chunk)
                    if sink is not None:
                        sink(chunk)
                    else:
                        chunks.append(chunk)
            done["body"] = b"".join(chunks)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller as it is
            done["error"] = exc

    thread = threading.Thread(target=work, daemon=True, name=f"http:{what}")
    thread.start()
    ticker = Ticker(what)
    while thread.is_alive():
        thread.join(POLL_S)  # a poll tick: the wait never gives up
        if thread.is_alive():
            ticker.what = f"{what} ({state['read'] // 1024} KB)" if state["read"] else what
            ticker.tick()
    if "error" in done:
        raise done["error"]
    body: bytes = done.get("body", b"")
    return body
