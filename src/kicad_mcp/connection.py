"""Thread-safe KiCad IPC connection management."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

import structlog
from kipy.board import Board
from kipy.kicad import KiCad

from .config import get_config
from .errors import KiCadBoardNotOpenError, KiCadMcpError, KiCadNotRunningError
from .kicad.session import KiCadSession


class KiCadConnectionError(KiCadNotRunningError):
    """Raised when KiCad IPC connection fails."""


logger = structlog.get_logger(__name__)
_lock = threading.RLock()
_session: KiCadSession | None = None
_kicad: object | None = None
_connection_generation = 0


_IPC_LOCK = threading.Lock()


def _one_request_at_a_time(*args: object, **kwargs: object) -> KiCad:
    """A kipy client whose requests go to KiCad one at a time.

    KiCad's IPC API is one request/reply socket. Tool calls running at once (a client that
    moves 21 footprints in parallel) shared it, and replies were cancelled ("Error
    receiving reply from KiCad: Operation canceled").
    """
    kicad = KiCad(*args, **kwargs)  # type: ignore[arg-type]
    client = getattr(kicad, "_client", None)
    send = getattr(client, "send", None)
    if send is not None:

        def serialized(command: object, response_type: object) -> object:
            with _IPC_LOCK:
                return send(command, response_type)

        client.send = serialized  # type: ignore[union-attr]
    return kicad


def _get_session() -> KiCadSession:
    """Return the process-wide KiCad session adapter."""
    global _session
    with _lock:
        if _session is None:
            _session = KiCadSession(client_factory=_one_request_at_a_time, logger=logger)
        return _session


def _connection_error(exc: KiCadMcpError) -> KiCadConnectionError:
    return KiCadConnectionError(
        str(exc)
        or (
            "Could not connect to KiCad IPC API.\n"
            "Make sure KiCad is running and the IPC API is enabled:\n"
            "  KiCad -> Preferences -> Scripting -> Enable IPC API Server\n"
            "If you use a custom socket or token, set:\n"
            "  KICAD_MCP_KICAD_SOCKET_PATH\n"
            "  KICAD_MCP_KICAD_TOKEN"
        )
    )


def _ensure_instance() -> bool:
    """With ``KICAD_MCP_KICAD_INSTANCE=auto``, attach to or start a KiCad for the board.

    Returns whether a KiCad now answers, after pointing the connection at it.
    """
    from .instance import get_instance_manager  # noqa: PLC0415
    from .instance.manager import _mode  # noqa: PLC0415

    if _mode() != "auto":
        return False
    cfg = get_config()
    status = get_instance_manager().ensure(cfg.pcb_file, cfg.kicad_cli)
    if not status.socket:
        raise KiCadConnectionError(f"{status.problem}\n{status.next_step}".strip())
    cfg.kicad_socket_path = status.socket
    reset_connection()
    return True


def get_kicad() -> KiCad:
    """Return a thread-safe KiCad IPC connection."""
    try:
        return _get_kicad()
    except KiCadConnectionError:
        if not _ensure_instance():
            raise
        return _get_kicad()


def _get_kicad() -> KiCad:
    global _kicad
    _ = get_config()
    try:
        _kicad = _get_session().client()
        return cast(KiCad, _kicad)
    except KiCadConnectionError:
        raise
    except KiCadMcpError as exc:
        raise _connection_error(exc) from exc


def get_board() -> Board:
    """Return the active board from KiCad, starting KiCad when the server may."""
    try:
        return _get_board()
    except KiCadConnectionError:
        if not _ensure_instance():
            raise
        return _get_board()


def _get_board() -> Board:
    try:
        return cast(Board, _get_session().board())
    except KiCadConnectionError:
        raise
    except KiCadBoardNotOpenError as exc:
        logger.debug("kicad_get_board_failed", error=str(exc))
        message = str(exc)
        if "busy" not in message.casefold():
            message = (
                "KiCad IPC is reachable, but no PCB is open in the active KiCad session.\n"
                "Open a .kicad_pcb file in KiCad or call kicad_set_project() to point the "
                "server at the expected project files."
            )
        raise KiCadConnectionError(message) from exc
    except KiCadMcpError as exc:
        raise _connection_error(exc) from exc


def get_connection_epoch() -> tuple[int, int]:
    """Return an internal epoch that changes after explicit reset or IPC disconnect."""
    with _lock:
        session_generation = _session.continuity_generation if _session is not None else -1
        return (_connection_generation, session_generation)


def reset_connection() -> None:
    """Force reconnect on next use."""
    global _session, _kicad, _connection_generation
    with _lock:
        _connection_generation += 1
        if _kicad is not None:
            close_fn = getattr(_kicad, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception as exc:
                    logger.debug("kicad_close_failed", error=str(exc))
        if _session is not None:
            _session.reset()
        _session = None
        _kicad = None


@contextmanager
def board_transaction() -> Generator[Board]:
    """Context manager for board operations."""
    with _lock:
        board = get_board()
        try:
            yield board
        except KiCadConnectionError:
            reset_connection()
            raise
