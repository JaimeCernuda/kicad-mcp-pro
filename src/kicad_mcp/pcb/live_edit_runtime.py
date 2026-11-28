"""Production accessor for each caller's native-live PCB mutation service."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Final

from kipy.board import Board

from ..caller import current_caller
from ..connection import KiCadConnectionError, get_board_to_edit, get_connection_epoch
from ..ipc.command_queue import get_command_queue
from .transaction_lifecycle import (
    PcbTransactionLifecycleService,
    PostconditionVerifier,
    SharedBoard,
    refuse_reload_with_open_groups,
)

REVIEWED_LIVE_MUTATIONS: Final[dict[str, bool]] = {
    "pcb_add_track": True,
    "pcb_add_tracks_bulk": False,
    "pcb_add_via": True,
    "pcb_add_blind_via": False,
    "pcb_add_microvia": False,
    "pcb_add_segment": False,
    "pcb_add_circle": False,
    "pcb_add_rectangle": False,
    "pcb_set_board_outline": False,
    "pcb_add_text": False,
    "pcb_delete_items": True,
    "pcb_refill_zones": False,
    "pcb_move_footprint": True,
    "pcb_set_footprint_layer": True,
    "pcb_add_zone": False,
    "pcb_set_keepout_zone": False,
    "pcb_add_teardrops": False,
    "route_single_track": False,
    "route_from_pad_to_pad": False,
    "pdn_generate_power_plane": False,
    "pcb_set_origin": False,
    "pcb_set_title_block_info": False,
}

_BOARD = SharedBoard()


def _run_mutation[T](operation: str, command: Callable[[], T]) -> T:
    """Execute one IPC mutation exactly once through the process-wide queue."""
    return get_command_queue().execute_mutation(
        operation,
        command,
        correlation_id=operation,
    )


def board_lock() -> threading.RLock:
    """The lock every change to the board holds, live or written to the board file."""
    return _BOARD.lock


def get_live_edit_service() -> PcbTransactionLifecycleService:
    """Return the calling session's board-bound native-live service.

    Each caller owns its edit group; KiCad keeps each caller's commit apart by client name.
    """
    caller = current_caller()
    with _BOARD.lock:
        service = _BOARD.groups.get(caller)
        if service is None:
            service = PcbTransactionLifecycleService(
                get_board=get_board_to_edit,
                run_mutation=_run_mutation,
                connection_errors=(KiCadConnectionError, OSError),
                get_connection_epoch=get_connection_epoch,
                shared=_BOARD,
            )
            _BOARD.groups[caller] = service
        return service


def refuse_board_reload(action: str) -> None:
    """Refuse ``action``, which reloads the board file, while any caller has a commit open."""
    with _BOARD.lock:
        refuse_reload_with_open_groups(_BOARD, _BOARD.groups.get(current_caller()), action)


def release_live_edit(same_board: bool) -> str:
    """Prepare for kicad_set_project or project creation; returns a note for the result.

    Reselecting the same board ends only the caller's own edit group: a KiCad commit it may
    still hold is dropped and its recovery state is cleared, while other sessions' groups stay
    open. Selecting another board is refused while any session has a commit open.
    """
    caller = current_caller()
    with _BOARD.lock:
        own = _BOARD.groups.get(caller)
        if not same_board:
            refuse_reload_with_open_groups(
                _BOARD,
                own,
                "Selecting another board",
                "moves this server off this board, which would leave {whose} staged changes "
                "out of reach in KiCad",
            )
        if own is None:
            return ""
        del _BOARD.groups[caller]
        return own.end_own_group()


def execute_live_board_mutation[T](
    operation: str,
    command: Callable[[Board], T],
    *,
    verifier: PostconditionVerifier[T] | None,
) -> T:
    """Execute one reviewed board mutation through the canonical service."""
    return get_live_edit_service().execute_board_mutation(
        operation,
        command,
        verifier=verifier,
        participates_in_live_session=REVIEWED_LIVE_MUTATIONS.get(operation, False),
    )


__all__ = [
    "REVIEWED_LIVE_MUTATIONS",
    "board_lock",
    "execute_live_board_mutation",
    "get_live_edit_service",
    "refuse_board_reload",
    "release_live_edit",
]
