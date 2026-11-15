"""The KiCad instance this server talks to: attached to, or started by the server.

By default (``KICAD_MCP_KICAD_INSTANCE=attach``) the server only connects to a KiCad
the user started, as it always has. With ``KICAD_MCP_KICAD_INSTANCE=auto`` the first
tool that needs KiCad's IPC API takes the first of these that works, and again after
the project changes:

1. ``attached``: a running KiCad with its API server on and this board open.
2. ``headless``: KiCad 11 or later (``kicad-cli api-server``) is started on the board.
3. ``xvfb``: on Linux with no display, ``pcbnew`` is started under ``xvfb-run``.
4. ``window``: with a display, ``pcbnew`` is started and its window appears.

A KiCad the server starts runs with its own ``KICAD_CONFIG_HOME``
(:mod:`kicad_mcp.instance.config_home`), so the user's own KiCad settings are never
written. It lives as long as the server and is stopped when the server exits; a KiCad
the server attached to is never stopped.

Every failure says what is missing and what to do; none is a stack trace.
"""

from __future__ import annotations

from .manager import (
    INSTANCE_ENV,
    InstanceManager,
    InstanceStatus,
    get_instance_manager,
)

__all__ = ["INSTANCE_ENV", "InstanceManager", "InstanceStatus", "get_instance_manager"]
