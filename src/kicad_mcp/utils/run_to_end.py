"""Run work to the end: no time limit, progress while it runs, and Stop ends it.

Nothing the connector runs gives up on its own after some seconds. A kicad-cli export on a
large board, a FreeRouting pass or KiCad starting on a slow machine takes as long as it
takes; while it runs, the tool call reports how long it has been running (MCP progress to
the host when the host asked for it, a log line otherwise), and it ends only when it
finishes or when the person or the agent stops the call. A stopped call stops its whole
process tree.

The tool's MCP ``Context`` is bound by ``KiCadMCPServer.call_tool`` (:func:`bound`); sync
tools run in a worker thread, where :mod:`anyio.from_thread` reaches the request's
cancellation and session.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import platform
import subprocess  # nosec B404 - argv-only, shell=False
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

import anyio
import anyio.from_thread
import structlog

logger = structlog.get_logger(__name__)

_WINDOWS = platform.system() == "Windows"
#: How often a running process is looked at (a poll, not a limit).
POLL_S = 0.5
#: How often progress is said while something runs.
SAY_EVERY_S = 1.0
#: Below this, a quick command reports nothing.
QUIET_FOR_S = 2.0

_REAL_RUN = subprocess.run

_context: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "kicad_mcp_tool_context", default=None
)


@contextlib.contextmanager
def bound(context: object | None) -> Iterator[None]:
    """Bind the running tool call's MCP ``Context`` for the work it starts."""
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def check_stopped() -> None:
    """Raise the host's cancellation if the call was stopped (a no-op outside a tool call)."""
    with contextlib.suppress(RuntimeError):  # not in an anyio worker thread
        anyio.from_thread.check_cancelled()


def say(what: str, seconds: float) -> None:
    """Say that ``what`` has been running for ``seconds`` (progress to the host, else a log)."""
    message = f"{what}: {int(seconds)} s"
    context = _context.get()
    if context is not None:
        with contextlib.suppress(Exception):  # progress is best-effort: the work goes on
            anyio.from_thread.run(context.report_progress, seconds, None, message)  # type: ignore[attr-defined]
            return
    if int(seconds) % 10 == 0:
        logger.info("still_running", what=what, seconds=int(seconds))


class Ticker:
    """Calls :func:`check_stopped` and :func:`say` from a loop that waits on something."""

    def __init__(self, what: str) -> None:
        self.what = what
        self.started = time.monotonic()
        self._said = 0.0

    def tick(self) -> None:
        check_stopped()
        waited = time.monotonic() - self.started
        if waited >= QUIET_FOR_S and waited - self._said >= SAY_EVERY_S:
            self._said = waited
            say(self.what, waited)


def _reader(stream: IO[bytes] | None, sink: list[bytes]) -> threading.Thread:
    def read() -> None:
        if stream is None:
            return
        with contextlib.suppress(OSError, ValueError):
            for chunk in iter(lambda: stream.read(65536), b""):
                sink.append(chunk)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


def _stop(process: subprocess.Popen[bytes]) -> None:
    from ..instance import processes  # noqa: PLC0415 - instance imports this module's users

    if process.poll() is None:
        processes.stop_tree(process.pid)


def run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    what: str = "",
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,  # noqa: A002 - the subprocess.run name
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` without a time limit: it waits for the command, however long.

    Output is captured. While it runs the call says so every second; when the tool call is
    stopped, the command's whole process tree is stopped and the cancellation propagates.
    """
    args = [str(part) for part in argv]
    if subprocess.run is not _REAL_RUN:
        # A test double of subprocess.run stands in for the process (the tests' seam).
        extra: dict[str, Any] = {}
        if cwd is not None:
            extra["cwd"] = str(cwd)
        if env is not None:
            extra["env"] = dict(env)
        if input is not None:
            extra["input"] = input
        return subprocess.run(  # noqa: S603  # nosec B603
            args, capture_output=True, text=text, timeout=None, check=False, **extra
        )
    label = what or Path(args[0]).name
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if _WINDOWS else 0  # type: ignore[attr-defined]
    process = subprocess.Popen(  # noqa: S603  # nosec B603 - argv only
        args,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=not _WINDOWS,
        creationflags=flags,
    )
    out: list[bytes] = []
    err: list[bytes] = []
    readers = [_reader(process.stdout, out), _reader(process.stderr, err)]
    if input is not None and process.stdin is not None:
        data = input.encode() if isinstance(input, str) else input
        with contextlib.suppress(OSError):
            process.stdin.write(data)
        with contextlib.suppress(OSError):
            process.stdin.close()
    ticker = Ticker(label)
    try:
        while True:
            try:
                process.wait(POLL_S)  # a poll tick: the loop never gives up
                break
            # limit-ok: the poll tick
            except subprocess.TimeoutExpired:
                ticker.tick()
    except BaseException:
        _stop(process)
        raise
    for reader in readers:
        reader.join()
    stdout: Any = b"".join(out)
    stderr: Any = b"".join(err)
    if text:
        stdout = stdout.decode(errors="replace")
        stderr = stderr.decode(errors="replace")
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def wait_process(process: subprocess.Popen[Any], what: str) -> int:
    """Wait for a process someone else started, however long; Stop stops its tree."""
    ticker = Ticker(what)
    try:
        while True:
            try:
                # limit-ok: the poll tick
                return process.wait(POLL_S)
            except subprocess.TimeoutExpired:
                ticker.tick()
    except BaseException:
        _stop(process)
        raise
