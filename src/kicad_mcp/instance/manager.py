"""Attach to a running KiCad, or start one: the cases of :mod:`kicad_mcp.instance`."""

from __future__ import annotations

import atexit
import contextlib
import getpass
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess  # nosec B404 - argv-only launches of KiCad binaries, shell=False
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

from ..kicad import ipc_watch
from ..utils import run_to_end
from . import config_home, processes
from .board_file import file_problem

logger = structlog.get_logger(__name__)

#: ``attach`` (the default): only connect to a KiCad the user started.
#: ``auto``: attach, or start KiCad when none serves the board.
INSTANCE_ENV = "KICAD_MCP_KICAD_INSTANCE"
#: Where the server keeps its private KiCad settings and sockets.
HOME_ENV = "KICAD_MCP_INSTANCE_HOME"

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
    #: The kicad-cli of the KiCad in use, when the server started it (exports match it).
    cli: str = ""
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
    # The managed KiCad is a disposable child of this server and its config home is seeded
    # on every start, so it lives in the per-user temp folder: writable on every OS and
    # under sandboxes that leave the home folder read-only.
    return Path(tempfile.gettempdir()) / f"kicad-mcp-{_user()}" / "instance"


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (a container without passwd entry)
        return str(os.getuid()) if hasattr(os, "getuid") else "user"


def default_socket() -> str:
    """kipy's default socket URL on this platform."""
    if platform.system() == "Windows":
        return f"ipc://{tempfile.gettempdir()}\\kicad\\api.sock"
    flatpak = Path.home() / ".var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock"
    if flatpak.exists():
        return f"ipc://{flatpak}"
    return "ipc:///tmp/kicad/api.sock"


#: A Unix socket path holds at most about 107 bytes; a longer one fails to bind and to dial
#: ("Address invalid"). A sandboxed client's temp folder alone can be 90 bytes long.
SOCKET_PATH_MAX = 100


def private_temp(home: Path) -> Path:
    """The temp folder a KiCad window started by this server gets, and no other process.

    Short on Linux and macOS (``/tmp/kmcp-<hash of the instance home>``), because the window's
    socket lives under it; on Windows the socket is a named pipe and has no such limit.
    """
    if platform.system() == "Windows":
        return home / "tmp"
    tag = hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:10]
    # Predictable on purpose (short); _own_private_temp refuses one another user made.
    short = Path("/tmp")  # noqa: S108
    base = short if os.access(short, os.W_OK) else Path(tempfile.gettempdir())
    return base / f"kmcp-{tag}"


def _own_private_temp(temp: Path) -> bool:
    """Create ``temp`` for this user only; refuse a symlink or a folder someone else owns."""
    try:
        temp.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = temp.lstat()
    except OSError:
        return False
    if temp.is_symlink() or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        return False
    with contextlib.suppress(OSError):
        temp.chmod(0o700)
    return True


def started_socket(home: Path) -> str:
    """The socket a KiCad window started by this server serves.

    KiCad puts its API socket under its temp folder (``<temp>/kicad/api.sock``), so every
    KiCad that shares a temp folder shares that path, and one that exits deletes the file,
    leaving another KiCad bound to a socket nobody can reach (seen when a second KiCad was
    started with the same temp folder). The window started here gets a temp folder of its own
    (:func:`private_temp`), so its socket is this server's alone.
    """
    if platform.system() == "Windows":
        return f"ipc://{private_temp(home)}\\kicad\\api.sock"
    return f"ipc://{private_temp(home) / 'kicad' / 'api.sock'}"


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


def probe(socket_url: str, timeout_ms: int = ipc_watch.NO_LIMIT_MS) -> Probe:
    """Ask a KiCad API socket for its version and open boards.

    A socket nobody listens on refuses at once; a KiCad that listens is waited for however
    long it takes to answer (a busy KiCad is still this board's KiCad, not an absent one),
    and the wait ends if that KiCad goes away (ipc_watch).
    """
    try:
        from kipy import KiCad  # noqa: PLC0415
        from kipy.proto.common.types import DocumentType  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - kipy is a dependency
        return Probe(False, error=f"kicad-python is not installed: {exc}")
    try:
        client = ipc_watch.watch(KiCad(socket_path=socket_url, timeout_ms=timeout_ms), socket_url)
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
    """The CLIs to try: only the one the user named, else the stable ones on ``PATH``.

    A development build (``kicad-cli-nightly``, or a version whose minor is 99) is used only
    when the user names it: someone with a nightly installed beside a release gets the
    release, never a nightly they did not choose.
    """
    found: list[Path] = []
    if configured is not None and configured.exists():
        found.append(configured)
        if os.environ.get("KICAD_CLI_PATH") or os.environ.get("KICAD_MCP_KICAD_CLI"):
            return found
    if (path := shutil.which("kicad-cli")) is not None and Path(path) not in found:
        if not _is_development(Path(path)):
            found.append(Path(path))
    return found


def _is_development(cli: Path) -> bool:
    """A nightly or development build: by its name, or a ``x.99`` version."""
    if "nightly" in str(cli.resolve()).casefold():
        return True
    parts = _version(cli).split(".")
    return len(parts) > 1 and parts[1].startswith("99")


def _supports_api_server(cli: Path) -> bool:
    try:
        result = run_to_end.run([str(cli), "--help"])
    except OSError:
        return False
    return "api-server" in (result.stdout + result.stderr)


def _version(cli: Path) -> str:
    try:
        result = run_to_end.run([str(cli), "version"])
    except OSError:
        return ""
    return str(result.stdout or result.stderr).strip()


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
        result = run_to_end.run([ps, "-eo", "args"])
    except OSError:
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
        self._watcher: subprocess.Popen[bytes] | None = None
        self._status = InstanceStatus(mode=_mode())
        atexit.register(self.stop)

    def isolate_settings(self, configured_cli: Path | None) -> str:
        """In ``auto`` mode, keep every KiCad this server runs off the person's settings.

        The window or headless server it starts already uses the private settings folder;
        this points ``KICAD_CONFIG_HOME`` there for the whole process too, so ``kicad-cli``
        exports, checks and the 3D model do not rewrite the person's ``pcbnew.json`` and
        friends. A ``KICAD_CONFIG_HOME`` the person set is kept. Returns the folder used,
        or ``""`` when nothing was changed.
        """
        if _mode() != "auto" or os.environ.get("KICAD_CONFIG_HOME"):
            return ""
        clis = _cli_candidates(configured_cli)
        cli = clis[0] if clis else None
        config = _home() / "config"
        config_home.seed(config, _version(cli) if cli is not None else None, cli)
        os.environ["KICAD_CONFIG_HOME"] = str(config)
        return str(config)

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
            if self._status.case == NONE and self._started is None:
                # Nothing recorded yet (no project set, nothing started): the person's own
                # KiCad may already be serving, and IPC talks to it. Say so; start nothing.
                for url in self._attach_candidates():
                    found = probe(url)
                    if found.ready:
                        self._status.case, self._status.version = ATTACHED, found.version
                        self._status.socket = url
                        self._status.board = ", ".join(found.boards)
                        break
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
            problem = file_problem(board)
            if problem:  # KiCad would stop on a dialog nobody sees; say what is wrong
                status.problem = f"KiCad was not started: {problem}"
                status.next_step = (
                    "Restore the board from the project's -backups folder or from version "
                    "control; KiCad cannot open it as it is."
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
        return self._launch(argv, case, board, home, version, cli, started_socket(home), status)

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
        stop_left_over(home)
        config = home / "config"
        config_home.seed(config, version, cli)
        env = {**os.environ, "KICAD_CONFIG_HOME": str(config)}
        if case != HEADLESS:  # a window's socket follows its temp folder: give it its own
            temp = private_temp(home)
            sock_path = socket_url.removeprefix("ipc://")
            if platform.system() != "Windows" and len(sock_path.encode()) > SOCKET_PATH_MAX:
                status.case = NONE
                status.problem = (
                    f"KiCad's API socket path would be {len(sock_path.encode())} bytes "
                    f"({sock_path}), longer than a Unix socket allows."
                )
                status.next_step = "Set KICAD_MCP_INSTANCE_HOME, or TMPDIR, to a shorter folder."
                return status
            if not _own_private_temp(temp):
                status.case = NONE
                status.problem = f"The private folder {temp} belongs to someone else or is a link."
                status.next_step = "Remove it, or set KICAD_MCP_INSTANCE_HOME to another folder."
                return status
            env.update(TMPDIR=str(temp), TMP=str(temp), TEMP=str(temp))
        log = home / f"kicad-{case}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        status.config_home, status.version, status.case = str(config), version, case
        status.cli = str(cli) if cli is not None else ""
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
        _record_started(home, process.pid)
        self._watcher = _watch(process.pid)
        status.pid, status.started_by_server = process.pid, True
        # Wait as long as KiCad is starting: it either answers or exits (a dialog nobody
        # sees is caught before the start, board_file). The wait is logged as it goes.
        started_at = time.monotonic()
        said = 0.0
        while True:
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
            waited = time.monotonic() - started_at
            if waited - said >= 10:
                said = waited
                logger.info(
                    "kicad_instance_still_starting",
                    seconds=round(waited),
                    socket=socket_url,
                    last_error=found.error,
                )
            time.sleep(1.0)

    def stop(self) -> None:
        """Stop the KiCad this server started; never one it attached to."""
        with self._lock:
            self._stop_started()

    def _stop_started(self) -> None:
        started, self._started = self._started, None
        if started is None or started.process.poll() is not None:
            return
        _record_started(_home(), None)
        processes.stop_tree(started.process.pid)  # its group (POSIX) or tree (Windows)
        # limit-ok: reaping after the forced stop
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            started.process.wait(timeout=5)  # reaping after the forced stop, not a limit
        temp = private_temp(_home())
        if not temp.is_symlink():  # KiCad's own temp files: nothing in it outlives KiCad
            shutil.rmtree(temp, ignore_errors=True)
        watcher, self._watcher = self._watcher, None
        if watcher is not None and watcher.stdin is not None:
            with contextlib.suppress(OSError):
                watcher.stdin.close()  # KiCad is stopped: the watcher sees the pipe close and exits


# ---------------------------------------------------------------------- lifetime
#
# atexit covers a normal exit only. A SIGTERM, SIGKILL or crash skips it, and the started
# KiCad runs in its own session, so it outlived the server. A watcher (``watch.py``) stops it
# when the server's pipe closes; the record lets the next start stop one a watcher missed.

_RECORD = "started.json"


def _watch(pid: int) -> subprocess.Popen[bytes] | None:
    """Start the watcher that stops KiCad's group or tree when this server is gone."""
    detach: dict[str, object] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if platform.system() == "Windows"
        else {"start_new_session": True}
    )
    try:
        return subprocess.Popen(  # noqa: S603  # nosec B603
            [sys.executable, "-m", "kicad_mcp.instance.watch", str(pid)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detach,  # type: ignore[arg-type]
        )
    except OSError:
        return None


def _record_started(home: Path, pid: int | None) -> None:
    record = home / _RECORD
    with contextlib.suppress(OSError):
        if pid is None:
            record.unlink(missing_ok=True)
        else:
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({"pid": pid, "server": os.getpid()}), encoding="utf-8")


def stop_left_over(home: Path) -> bool:
    """Stop a KiCad recorded in ``home`` by a server that is gone; ``True`` when one was."""
    record = home / _RECORD
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        pid, server = int(data["pid"]), int(data["server"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if server == os.getpid() or processes.command_line(server):
        return False  # this server's own KiCad, or another live server's
    stopped = False
    if processes.alive(pid) and processes.is_kicad(pid):
        processes.stop_tree(pid)
        stopped = True
    _record_started(home, None)
    return stopped


_manager: InstanceManager | None = None
_manager_lock = threading.Lock()


def get_instance_manager() -> InstanceManager:
    """The process-wide instance manager."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = InstanceManager()
        return _manager
