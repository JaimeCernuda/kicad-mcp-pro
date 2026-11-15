"""Attach to a running KiCad, or start one: the cases of :mod:`kicad_mcp.instance`."""

from __future__ import annotations

import atexit
import contextlib
import os
import platform
import shutil
import signal
import socket
import subprocess  # nosec B404 - argv-only launches of KiCad binaries, shell=False
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config_home

#: ``attach`` (the default): only connect to a KiCad the user started.
#: ``auto``: attach, or start KiCad when none serves the board.
INSTANCE_ENV = "KICAD_MCP_KICAD_INSTANCE"
#: Where the server keeps its private KiCad settings and sockets.
HOME_ENV = "KICAD_MCP_INSTANCE_HOME"
#: Seconds to wait for a started KiCad to answer.
START_TIMEOUT_ENV = "KICAD_MCP_INSTANCE_START_TIMEOUT"

ATTACHED = "attached"
HEADLESS = "headless"
XVFB = "xvfb"
WINDOW = "window"
NONE = "none"

_NOT_READY = "not ready"


@dataclass
class InstanceStatus:
    """What the server's KiCad is, and why when there is none."""

    case: str = NONE
    mode: str = "attach"
    version: str = ""
    pid: int | None = None
    board: str = ""
    socket: str = ""
    config_home: str = ""
    started_by_server: bool = False
    #: What is missing (KiCad, xvfb-run, a display, a board file) and what to do.
    problem: str = ""
    next_step: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Started:
    process: subprocess.Popen[bytes]
    case: str
    board: Path
    socket: str
    log: Path


def _mode() -> str:
    value = os.environ.get(INSTANCE_ENV, "attach").strip().casefold()
    return value if value in ("attach", "auto") else "attach"


def _home() -> Path:
    raw = os.environ.get(HOME_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "kicad-mcp" / "instance"


def _start_timeout() -> float:
    try:
        return max(5.0, float(os.environ.get(START_TIMEOUT_ENV, "90")))
    except ValueError:
        return 90.0


def default_socket() -> str:
    """kipy's default socket URL on this platform."""
    if platform.system() == "Windows":
        return f"ipc://{tempfile.gettempdir()}\\kicad\\api.sock"
    flatpak = Path.home() / ".var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock"
    if flatpak.exists():
        return f"ipc://{flatpak}"
    return "ipc:///tmp/kicad/api.sock"


def _socket_url(path_or_url: str) -> str:
    return path_or_url if "://" in path_or_url else f"ipc://{path_or_url}"


@dataclass(frozen=True)
class Probe:
    """What a socket answered."""

    reachable: bool
    ready: bool = False
    version: str = ""
    boards: tuple[str, ...] = ()
    error: str = ""


def probe(socket_url: str, timeout_ms: int = 3000) -> Probe:
    """Ask a KiCad API socket for its version and open boards."""
    try:
        from kipy import KiCad  # noqa: PLC0415
        from kipy.proto.common.types import DocumentType  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - kipy is a dependency
        return Probe(False, error=f"kicad-python is not installed: {exc}")
    try:
        client = KiCad(socket_path=socket_url, timeout_ms=timeout_ms)
        version = client.get_version()
    except Exception as exc:  # noqa: BLE001 - kipy raises its own and transport errors
        text = str(exc)
        return Probe(_NOT_READY in text.casefold(), error=text)
    try:
        docs = client.get_open_documents(DocumentType.DOCTYPE_PCB)
        boards = tuple(str(doc.board_filename) for doc in docs)
    except Exception as exc:  # noqa: BLE001
        return Probe(True, True, str(getattr(version, "full_version", version)), error=str(exc))
    return Probe(True, True, str(getattr(version, "full_version", version)), boards)


def _serves(found: Probe, board: Path) -> bool:
    return any(Path(name).name == board.name for name in found.boards)


def _has_display() -> bool:
    if platform.system() in ("Windows", "Darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _cli_candidates(configured: Path | None) -> list[Path]:
    """The CLIs to try: only the one the user named, else every one on ``PATH``."""
    names = ["kicad-cli", "kicad-cli-nightly"]
    found: list[Path] = []
    if configured is not None and configured.exists():
        found.append(configured)
        if os.environ.get("KICAD_CLI_PATH") or os.environ.get("KICAD_MCP_KICAD_CLI"):
            return found
    for name in names:
        if (path := shutil.which(name)) is not None and Path(path) not in found:
            found.append(Path(path))
    return found


def _supports_api_server(cli: Path) -> bool:
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [str(cli), "--help"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "api-server" in (result.stdout + result.stderr)


def _version(cli: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [str(cli), "version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr).strip()


def _pcbnew_for(cli: Path | None) -> Path | None:
    candidates: list[Path] = []
    if cli is not None:
        resolved = cli.resolve()
        for name in ("pcbnew", "pcbnew.exe"):
            candidates.append(resolved.parent / name)
        # macOS bundle: KiCad.app/Contents/MacOS/kicad-cli and
        # KiCad.app/Contents/Applications/pcbnew.app/Contents/MacOS/pcbnew
        candidates.append(resolved.parent.parent / "Applications/pcbnew.app/Contents/MacOS/pcbnew")
    for path in candidates:
        if path.is_file():
            return path
    found = shutil.which("pcbnew")
    return Path(found) if found else None


def clear_stale_locks(board: Path, running: Callable[[Path], bool]) -> list[str]:
    """Remove KiCad lock files on ``board``'s project left by a KiCad that is gone.

    A lock names a host and a user, not a process: one is stale when it names this
    host and no running KiCad has the board open.
    """
    if running(board):
        return []
    removed: list[str] = []
    host = socket.gethostname()
    for lock in board.parent.glob(f"~{board.stem}.*.lck"):
        try:
            text = lock.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if host in text or not text.strip():
            with contextlib.suppress(OSError):
                lock.unlink()
                removed.append(lock.name)
    return removed


def _kicad_has_open(board: Path) -> bool:
    """Whether a KiCad process on this machine was started on ``board``."""
    ps = shutil.which("ps")
    if platform.system() == "Windows" or ps is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [ps, "-eo", "args"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(
        str(board) in line and ("pcbnew" in line or "kicad" in line)
        for line in result.stdout.splitlines()
    )


class InstanceManager:
    """The one KiCad instance this server process uses."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started: _Started | None = None
        self._status = InstanceStatus(mode=_mode())
        atexit.register(self.stop)

    # ------------------------------------------------------------------ reading

    def status(self) -> InstanceStatus:
        with self._lock:
            self._status.mode = _mode()
            if self._started is not None and self._started.process.poll() is not None:
                self._status.notes.append(
                    f"The KiCad the server started exited with code "
                    f"{self._started.process.returncode}; see {self._started.log}."
                )
                self._started = None
                self._status.case = NONE
                self._status.pid = None
            return self._status

    # ------------------------------------------------------------------ acting

    def ensure(self, board: Path | None, configured_cli: Path | None = None) -> InstanceStatus:
        """Attach to or start a KiCad serving ``board``; never raises."""
        with self._lock:
            status = InstanceStatus(mode=_mode())
            if board is None or not board.is_file():
                status.problem = "No board file is set for this project."
                status.next_step = (
                    "Call kicad_set_project with a folder that has a .kicad_pcb file."
                )
                self._status = status
                return status
            board = board.resolve()
            status.board = str(board)
            started = self._started
            if started is not None and started.process.poll() is None and started.board == board:
                return self._status
            for url in self._attach_candidates():
                found = probe(url)
                if found.ready and _serves(found, board):
                    if started is not None and started.socket != url:
                        self._stop_started()
                    status.case, status.version, status.socket = ATTACHED, found.version, url
                    self._status = status
                    return status
            if status.mode != "auto":
                status.problem = "No running KiCad has this board open with its API server on."
                status.next_step = (
                    "Open the board in KiCad with Preferences > Plugins > Enable KiCad API on, "
                    f"or set {INSTANCE_ENV}=auto to let the server start KiCad."
                )
                self._status = status
                return status
            if started is not None:
                self._stop_started()
            self._status = self._start(board, configured_cli, status)
            return self._status

    def _attach_candidates(self) -> list[str]:
        urls: list[str] = []
        configured = os.environ.get("KICAD_API_SOCKET") or os.environ.get(
            "KICAD_MCP_KICAD_SOCKET_PATH"
        )
        if configured:
            urls.append(_socket_url(configured))
        urls.append(default_socket())
        return list(dict.fromkeys(urls))

    def _start(
        self, board: Path, configured_cli: Path | None, status: InstanceStatus
    ) -> InstanceStatus:
        clis = _cli_candidates(configured_cli)
        if not clis and _pcbnew_for(None) is None:
            status.problem = "KiCad was not found on this machine."
            status.next_step = (
                "Install KiCad 10 or later (https://www.kicad.org/download/), or point "
                "KICAD_CLI_PATH at the kicad-cli of an install the server did not find."
            )
            return status
        home = _home()
        removed = clear_stale_locks(board, _kicad_has_open)
        if removed:
            status.notes.append(f"Removed stale KiCad locks: {', '.join(removed)}.")
        headless = next((cli for cli in clis if _supports_api_server(cli)), None)
        if headless is not None:
            return self._launch_headless(headless, board, home, status)
        cli = clis[0] if clis else None
        pcbnew = _pcbnew_for(cli)
        if pcbnew is None:
            status.problem = "KiCad's PCB editor (pcbnew) was not found next to kicad-cli."
            status.next_step = "Install the full KiCad application, not only kicad-cli."
            return status
        argv = [str(pcbnew), str(board)]
        case = WINDOW
        if not _has_display():
            xvfb = shutil.which("xvfb-run")
            if xvfb is None:
                status.problem = (
                    "There is no display, and KiCad 10's editor needs one; xvfb-run was not found."
                )
                status.next_step = (
                    "Install Xvfb (for example apt-get install xvfb), or install KiCad 11 or "
                    "later, whose kicad-cli api-server needs no display."
                )
                return status
            argv = [xvfb, "-a", "-s", "-screen 0 1600x1000x24", *argv]
            case = XVFB
        version = _version(cli) if cli is not None else ""
        return self._launch(argv, case, board, home, version, cli, default_socket(), status)

    def _launch_headless(
        self, cli: Path, board: Path, home: Path, status: InstanceStatus
    ) -> InstanceStatus:
        run_dir = home / "sockets"
        run_dir.mkdir(parents=True, exist_ok=True)
        sock = run_dir / f"api-{os.getpid()}.sock"
        with contextlib.suppress(OSError):
            sock.unlink()
        argv = [str(cli), "api-server", "--socket", str(sock), str(board)]
        return self._launch(
            argv, HEADLESS, board, home, _version(cli), cli, f"ipc://{sock}", status
        )

    def _launch(
        self,
        argv: list[str],
        case: str,
        board: Path,
        home: Path,
        version: str,
        cli: Path | None,
        socket_url: str,
        status: InstanceStatus,
    ) -> InstanceStatus:
        config = home / "config"
        config_home.seed(config, version, cli)
        env = {**os.environ, "KICAD_CONFIG_HOME": str(config)}
        log = home / f"kicad-{case}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        status.config_home, status.version, status.case = str(config), version, case
        with log.open("wb") as handle:
            try:
                process = subprocess.Popen(  # noqa: S603  # nosec B603
                    argv,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=platform.system() != "Windows",
                )
            except OSError as exc:
                status.case = NONE
                status.problem = f"KiCad could not be started: {exc}"
                status.next_step = "Check that the KiCad install is complete."
                return status
        self._started = _Started(process, case, board, socket_url, log)
        status.pid, status.started_by_server = process.pid, True
        deadline = time.monotonic() + _start_timeout()
        last = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                status.case = NONE
                status.problem = f"KiCad exited with code {process.returncode} while starting."
                status.next_step = f"Read {log} for KiCad's own message."
                self._started = None
                return status
            found = probe(socket_url)
            if found.ready and _serves(found, board):
                status.socket = socket_url
                status.version = found.version or version
                return status
            last = found.error or last
            time.sleep(1.0)
        status.problem = f"KiCad started but did not answer on {socket_url} in time ({last})."
        status.next_step = (
            f"Read {log}. A dialog may be open in KiCad's window; {START_TIMEOUT_ENV} sets "
            "how long the server waits."
        )
        return status

    def stop(self) -> None:
        """Stop the KiCad this server started; never one it attached to."""
        with self._lock:
            self._stop_started()

    def _stop_started(self) -> None:
        started, self._started = self._started, None
        if started is None or started.process.poll() is not None:
            return
        try:
            if platform.system() != "Windows":
                os.killpg(started.process.pid, signal.SIGTERM)
            else:
                started.process.terminate()
            started.process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(OSError):
                if platform.system() != "Windows":
                    os.killpg(started.process.pid, signal.SIGKILL)
                else:
                    started.process.kill()


_manager: InstanceManager | None = None
_manager_lock = threading.Lock()


def get_instance_manager() -> InstanceManager:
    """The process-wide instance manager."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = InstanceManager()
        return _manager
