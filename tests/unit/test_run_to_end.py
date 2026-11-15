"""Work runs to the end: no time limit, progress while it runs, and Stop stops it all."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import anyio.to_thread
import pytest

from kicad_mcp.utils import http_to_end, run_to_end

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


class _Context:
    """Stands in for the MCP Context: records the progress it is given."""

    def __init__(self) -> None:
        self.progress: list[tuple[float, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress.append((progress, message))


def test_a_command_runs_however_long_it_takes_and_its_output_is_kept() -> None:
    started = time.monotonic()
    result = run_to_end.run(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(2.5); print('done'); "
            "import sys; print('note', file=sys.stderr)",
        ],
        what="slow command",
    )
    assert time.monotonic() - started >= 2.5
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (0, "done", "note")


def test_progress_reaches_the_tool_call_while_it_runs() -> None:
    context = _Context()

    async def main() -> None:
        with run_to_end.bound(context):
            await anyio.to_thread.run_sync(
                lambda: run_to_end.run(
                    [sys.executable, "-c", "import time; time.sleep(4)"], what="kicad-cli pcb drc"
                )
            )

    anyio.run(main)
    messages = [message for _, message in context.progress]
    assert messages and all(m and m.startswith("kicad-cli pcb drc: ") for m in messages)
    assert context.progress[-1][0] >= 3


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"


def test_stopping_the_call_stops_the_whole_process_tree(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = f"sleep 300 & echo $! > {pids}; sleep 300"

    async def main() -> None:
        with anyio.move_on_after(2):  # the person presses Stop after 2 s
            await anyio.to_thread.run_sync(lambda: run_to_end.run(["sh", "-c", script]))

    anyio.run(main)
    child = int(pids.read_text())
    for _ in range(50):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child)


class _Slow(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        time.sleep(1.5)  # the answer takes longer than any tick
        self.send_response(200)
        self.end_headers()
        for _ in range(3):
            self.wfile.write(b"x" * 1000)
            self.wfile.flush()
            time.sleep(1.0)


@pytest.fixture
def slow_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def test_a_slow_answer_is_read_to_the_end(slow_server: str) -> None:
    assert http_to_end.fetch(slow_server, what="parts search") == b"x" * 3000


def test_stopping_the_call_ends_a_slow_request(slow_server: str) -> None:
    async def main() -> None:
        with anyio.move_on_after(1):
            await anyio.to_thread.run_sync(lambda: http_to_end.fetch(slow_server, what="search"))

    started = time.monotonic()
    anyio.run(main)
    assert time.monotonic() - started < 3
