"""FastMCP adapter for the KiCad instance the server attaches to or starts."""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer as FastMCP

from ..config import get_config
from ..instance import get_instance_manager
from ..instance.manager import _mode
from .metadata import headless_compatible

_CASE_WORDS = {
    "attached": "attached to a KiCad that was already running",
    "headless": "started KiCad's headless API server (kicad-cli api-server)",
    "xvfb": "started KiCad's PCB editor on a virtual display (Xvfb)",
    "window": "started KiCad's PCB editor in a window",
    "none": "no KiCad instance yet",
}


def register(mcp: FastMCP) -> None:
    """Register the instance status tool."""

    @mcp.tool()
    @headless_compatible
    def kicad_session_status(start: bool = False) -> str:
        """Report which KiCad this server uses, how it got it, and what is missing.

        Shows the case (attached, headless, xvfb, window or none), KiCad's version,
        the process the server started, the board, the private settings folder and
        what to do when there is no KiCad. ``start`` attaches to or starts KiCad now
        (with ``KICAD_MCP_KICAD_INSTANCE=auto``) instead of on the first tool that
        needs it.
        """
        manager = get_instance_manager()
        cfg = get_config()
        if start:
            status = manager.ensure(cfg.pcb_file, cfg.kicad_cli)
            if status.socket:
                cfg.kicad_socket_path = status.socket
                from ..connection import reset_connection  # noqa: PLC0415

                reset_connection()
        else:
            status = manager.status()
        lines = [f"KiCad instance: {_CASE_WORDS.get(status.case, status.case)}."]
        if status.version:
            lines.append(f"Version: {status.version}.")
        if status.board:
            lines.append(f"Board: {status.board}.")
        if status.pid:
            lines.append(f"Process {status.pid}, started by this server; it stops with the server.")
        if status.config_home:
            lines.append(f"Private KiCad settings: {status.config_home}.")
        if status.problem:
            lines.append(f"Problem: {status.problem}")
        if status.next_step:
            lines.append(f"Next step: {status.next_step}")
        if not status.socket and not status.problem and _mode() != "auto":
            lines.append(
                "The server only attaches to a running KiCad "
                "(KICAD_MCP_KICAD_INSTANCE=attach); set it to auto to let the server start one."
            )
        lines.extend(status.notes)
        lines.append("Status: " + json.dumps(status.as_dict()))
        return "\n".join(lines)
