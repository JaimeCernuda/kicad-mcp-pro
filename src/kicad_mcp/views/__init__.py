"""MCP Apps views of the project: board, schematic, BOM, checks, stackup and parts.

Opt-in with ``KICAD_MCP_ENABLE_APPS=1``: the server then registers six ``ui://kicad/*``
resources (one self-contained HTML page each, from ``data/views/kicad-views.html``),
binds the opening tools to them with ``_meta.ui.resourceUri``, marks the data tools as
visible to the view only (``_meta.ui.visibility = ["app"]``), and advertises the
``io.modelcontextprotocol/ui`` extension. Without it, the opening tools answer with a
text summary, the data tools still answer, and nothing about Apps is advertised, which
keeps ADR 0006's default (no ``ui://`` resources) for every host that does not opt in.

Every operation a view offers is a call to one of the server's own tools; the views hold
no edit logic.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.server.mcpserver.resources.types import TextResource
from mcp.types import CallToolResult, TextContent

from ..config import get_config
from . import data
from .revisions import RevisionTracker, digest_of, file_bytes

APPS_ENV = "KICAD_MCP_ENABLE_APPS"
EXTENSION_ID = "io.modelcontextprotocol/ui"
APP_MIME_TYPE = "text/html;profile=mcp-app"
VIEWS = ("board", "schematic", "bom", "checks", "stackup", "parts")


def apps_enabled() -> bool:
    return os.environ.get(APPS_ENV, "").strip().casefold() in ("1", "true", "yes", "on")


def view_uri(view: str) -> str:
    return f"ui://kicad/{view}"


def view_html(view: str) -> str:
    """The bundle with this view's name set before its script runs."""
    page = resources.files("kicad_mcp").joinpath("data/views/kicad-views.html").read_text("utf-8")
    marker = '<div id="kicad-views"></div>'
    return page.replace(marker, f'{marker}\n<script>window.KICAD_VIEW = "{view}";</script>', 1)


def _fingerprint() -> tuple[str, str]:
    cfg = get_config()
    try:
        root = data.project_root()
    except ValueError:
        return "", ""
    parts: list[bytes] = []
    text = data.live_board_text()
    if text is not None:
        parts.append(text.encode("utf-8"))
    elif cfg.pcb_file is not None:
        parts.extend(file_bytes([Path(cfg.pcb_file)]))
    parts.extend(file_bytes(data.schematic_paths()))
    return str(root), digest_of(*parts)


_tracker = RevisionTracker(_fingerprint)


def _use_project(project: str) -> None:
    """Point the server at ``project`` when a view names another one (KV9 reopening)."""
    if not project:
        return
    cfg = get_config()
    target = Path(project).expanduser()
    current = Path(cfg.project_dir) if cfg.project_dir is not None else None
    if current is not None and target.resolve() == current.resolve():
        return
    cfg.apply_project(target)


def _meta(view: str, *, app_only: bool = False) -> dict[str, Any] | None:
    if not apps_enabled():
        return None
    ui: dict[str, Any] = {"resourceUri": view_uri(view)}
    if app_only:
        ui["visibility"] = ["app"]
    return {"ui": ui}


def _with_revision(build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    payload = build()
    payload["revision"] = _tracker.current()
    return payload


def register(mcp: FastMCP) -> None:
    """Register the view tools, and the ``ui://`` resources when Apps are enabled."""
    enabled = apps_enabled()
    if enabled:
        for view in VIEWS:
            mcp.add_resource(
                TextResource(
                    uri=view_uri(view),
                    name=f"kicad-{view}",
                    title=f"KiCad {view}",
                    description=f"The project's {view} view.",
                    mime_type=APP_MIME_TYPE,
                    text=view_html(view),
                    meta={"ui": {"csp": {}, "prefersBorder": False}},
                )
            )
        lowlevel = getattr(mcp, "_lowlevel_server", None)
        if lowlevel is not None and isinstance(getattr(lowlevel, "extensions", None), dict):
            lowlevel.extensions[EXTENSION_ID] = {"mimeTypes": [APP_MIME_TYPE]}

    # ------------------------------------------------------------ opening tools

    def opened(view: str, extra: str = "", **arguments: object) -> CallToolResult:
        """The opening answer: text for the model, and the project for the view.

        ``structuredContent.arguments`` carries the project the view was opened on, so the
        view's data calls name it even when the server restarted in between.
        """
        cfg = get_config()
        name = Path(cfg.project_dir).name if cfg.project_dir is not None else "the project"
        how = "in a tab" if enabled else "as text only (this server has no views enabled)"
        text = f"Opened the {view} view of {name} {how}.{(' ' + extra) if extra else ''}"
        if cfg.restored_project is not None and cfg.restored_project == cfg.project_dir:
            text += " (The project was set again from this folder's last kicad_set_project.)"
        if cfg.project_dir is not None:
            arguments = {"project": str(cfg.project_dir), **arguments}
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent={"text": text, "arguments": arguments},
        )

    @mcp.tool(meta=_meta("board"))
    def view_board(
        project: str = "", layers: list[str] | None = None, focus: str = "", mode: str = "board"
    ) -> CallToolResult:
        """Open the board in a view the person can see and work in: layers, nets, 2D and 3D.

        ``mode`` ``3d`` opens on the 3D model with component bodies, which the person can turn;
        ``board`` (the default) on the drawing. ``focus`` centres a reference (U1), a net (GND)
        or nothing in the new view. Each call opens another view. A view that is already open
        follows every edit and offers its own tools (``show`` to switch between the drawing and
        3D, ``focus``, ``set_layers``, ``select``, ``capture``) to steer it in place. The view
        shows the live KiCad's board; the person can drag, rotate, flip and lock footprints
        there, and what they select reaches you as context.
        """
        _use_project(project)
        board = data.board_data()
        counts = data.board_summary(board["text"])
        origin = "live KiCad" if board["source"] == "live" else "board file"
        extra = (
            f"{counts['footprints']} footprints, {counts['nets']} nets, {counts['tracks']} track "
            f"segments; board from the {origin}."
        )
        return opened("board", extra, layers=layers or [], focus=focus, mode=mode)

    @mcp.tool(meta=_meta("schematic"))
    def view_schematic(project: str = "", sheet: str = "", focus: str = "") -> CallToolResult:
        """Open the schematic in a view: every sheet, symbol selection and "not fitted"."""
        _use_project(project)
        sheets = data.schematic_data()["files"]
        return opened("schematic", f"{len(sheets)} sheet files.", sheet=sheet, focus=focus)

    @mcp.tool(meta=_meta("bom"))
    def view_bom(project: str = "", quantity: int = 1) -> CallToolResult:
        """Open the bill of materials with stock, price, lifecycle, datasheets and key specs.

        From the view the person can see alternates, choose one, mark parts not fitted and
        reprice at ``quantity`` boards.
        """
        _use_project(project)
        rows = data.bom_data(quantity=quantity, sourcing=False)["rows"]
        return opened("bom", f"{len(rows)} rows.", quantity=quantity)

    @mcp.tool(meta=_meta("checks"))
    def view_checks(project: str = "", kind: str = "both") -> CallToolResult:
        """Open DRC and ERC results grouped by rule (``kind``: drc, erc or both)."""
        _use_project(project)
        if kind not in ("drc", "erc", "both"):
            return CallToolResult(
                content=[
                    TextContent(type="text", text="View failed: kind must be drc, erc or both.")
                ],
                isError=True,
            )
        result = data.checks_data(kind)
        summary = "; ".join(f"{name.upper()} {text}" for name, text in result["summaries"].items())
        return opened("checks", summary, kind=kind)

    @mcp.tool(meta=_meta("stackup"))
    def view_stackup(project: str = "") -> CallToolResult:
        """Open the board's stackup and net classes; the person can edit thicknesses there."""
        _use_project(project)
        layers = data.stackup_data()["layers"]
        return opened("stackup", f"{len(layers)} layers.")

    @mcp.tool(meta=_meta("parts"))
    def view_parts(
        query: str = "",
        category: str = "",
        filters: list[dict[str, Any]] | None = None,
        candidates: list[str] | None = None,
        package: str = "",
        symbol: str = "",
    ) -> CallToolResult:
        """Open a parts table with specs, filters, column choice and side-by-side compare.

        Give ``category`` (and ``filters`` as for lib_search_parametric) to search, or your
        shortlist in ``candidates`` (MPNs or LCSC codes). ``symbol`` (U2) is where the
        person's pick is bound.
        """
        _ = query, filters, package, symbol
        shown = (
            f"{len(candidates)} candidates"
            if candidates
            else f"category {category or query or '(none yet)'}"
        )
        return opened("parts", f"{shown}.")

    # ------------------------------------------------------------ data tools

    @mcp.tool(meta=_meta("board", app_only=True))
    def view_data_board(project: str = "") -> dict[str, Any]:
        """Board text and revision for the board view (for the view, not the model)."""
        _use_project(project)
        return _with_revision(data.board_data)

    @mcp.tool(meta=_meta("schematic", app_only=True))
    def view_data_schematic(project: str = "") -> dict[str, Any]:
        """Schematic files and revision for the schematic view (for the view, not the model)."""
        _use_project(project)
        return _with_revision(data.schematic_data)

    @mcp.tool(meta=_meta("board", app_only=True))
    def view_data_glb(project: str = "") -> CallToolResult:
        """The board's 3D model (GLB, base64) for the board view's 3D tab."""
        _use_project(project)
        payload = _with_revision(data.glb_data)
        # The model goes once, as structured content: a text copy would double a result
        # that is already several megabytes.
        size = payload.get("size", 0)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"3D model of the board, {size} bytes, revision {payload['revision']}.",
                )
            ],
            structuredContent=payload,
        )

    @mcp.tool(meta=_meta("bom", app_only=True))
    def view_data_bom(project: str = "", quantity: int = 1) -> dict[str, Any]:
        """BOM rows with sourcing and specs for the BOM view (for the view, not the model)."""
        _use_project(project)
        return _with_revision(lambda: data.bom_data(quantity=max(1, quantity)))

    @mcp.tool(meta=_meta("checks", app_only=True))
    def view_data_checks(project: str = "", kind: str = "both", run: bool = True) -> dict[str, Any]:
        """DRC and ERC violations with positions for the checks view."""
        _use_project(project)
        return _with_revision(lambda: data.checks_data(kind, run))

    @mcp.tool(meta=_meta("stackup", app_only=True))
    def view_data_stackup(project: str = "") -> dict[str, Any]:
        """Stackup layers and net classes for the stackup view."""
        _use_project(project)
        return _with_revision(data.stackup_data)

    @mcp.tool(meta=_meta("parts", app_only=True))
    def view_data_parts(
        category: str = "",
        filters: list[dict[str, Any]] | None = None,
        package: str = "",
        min_stock: int = 0,
        candidates: list[str] | None = None,
    ) -> dict[str, Any]:
        """Part rows with specs for the parts view (for the view, not the model)."""
        return data.parts_data(category, filters or [], package, min_stock, candidates)

    @mcp.tool(meta=_meta("parts", app_only=True))
    def view_data_compare(parts: list[str], category: str = "") -> dict[str, Any]:
        """Parts side by side for the parts view's compare panel."""
        return data.compare_data(parts, category)

    @mcp.tool(meta=_meta("board", app_only=True))
    def view_wait_revision(
        project: str = "", since: int = 0, timeout_s: float = 20.0
    ) -> dict[str, Any]:
        """Wait until the project's revision passes ``since`` (or the timeout); for the views."""
        _use_project(project)
        revision, changed = _tracker.wait(since, timeout_s)
        return {"revision": revision, "changed": changed}
