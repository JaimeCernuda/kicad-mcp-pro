"""FastMCP adapter: footprint locking and board outlines from shapes or a DXF file."""

from __future__ import annotations

from typing import Any, cast

from kipy.board import Board
from kipy.board_types import BoardArc, BoardCircle, BoardItem, BoardSegment
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types.base_types_pb2 import LockedState
from mcp.server.mcpserver import MCPServer as FastMCP

from ..config import get_config
from ..pcb.dxf_outline import (
    DxfOutline,
    OutlineArc,
    OutlineHole,
    OutlineSegment,
    placed,
    read_dxf_outline,
)
from ..pcb.live_edit_runtime import execute_live_board_mutation
from ..utils.units import mm_to_nm
from .metadata import requires_kicad_running

_EDGE_WIDTH_MM = 0.05


def _edge_items(outline: DxfOutline) -> list[BoardItem]:
    items: list[BoardItem] = []
    for edge in outline.edges:
        if isinstance(edge, OutlineArc):
            arc = BoardArc()
            arc.layer = BoardLayer.BL_Edge_Cuts
            arc.start = Vector2.from_xy_mm(*edge.start)
            arc.mid = Vector2.from_xy_mm(*edge.mid)
            arc.end = Vector2.from_xy_mm(*edge.end)
            arc.attributes.stroke.width = mm_to_nm(_EDGE_WIDTH_MM)
            items.append(arc)
        else:
            segment = BoardSegment()
            segment.layer = BoardLayer.BL_Edge_Cuts
            segment.start = Vector2.from_xy_mm(*edge.start)
            segment.end = Vector2.from_xy_mm(*edge.end)
            segment.attributes.stroke.width = mm_to_nm(_EDGE_WIDTH_MM)
            items.append(segment)
    for hole in outline.holes:
        circle = BoardCircle()
        circle.layer = BoardLayer.BL_Edge_Cuts
        circle.center = Vector2.from_xy_mm(*hole.center)
        circle.radius_point = Vector2.from_xy_mm(hole.center[0] + hole.diameter / 2, hole.center[1])
        circle.attributes.stroke.width = mm_to_nm(_EDGE_WIDTH_MM)
        items.append(circle)
    return items


def _draw(board: Board, outline: DxfOutline, replace: bool) -> str:
    existing = [shape for shape in board.get_shapes() if shape.layer == BoardLayer.BL_Edge_Cuts]
    if existing and not replace:
        return (
            f"Board outline unchanged: Edge.Cuts already has {len(existing)} shapes. "
            "Call again with replace=true to remove them and draw the new outline."
        )
    if existing:
        board.remove_items(existing)
    created = list(board.create_items(_edge_items(outline)))
    bounds = outline.bounds()
    size = f"; {bounds[2] - bounds[0]:.2f} x {bounds[3] - bounds[1]:.2f} mm" if bounds else ""
    removed = f" Removed {len(existing)} previous Edge.Cuts shapes." if existing else ""
    return (
        f"Board outline drawn: {len(outline.edges)} edges and {len(outline.holes)} holes "
        f"on Edge.Cuts ({len(created)} items){size}.{removed}"
    )


def _outline_from_points(points: list[dict[str, Any]]) -> DxfOutline:
    if len(points) < 3:
        raise ValueError("An outline needs at least three points.")
    outline = DxfOutline()
    for index, point in enumerate(points):
        start = (float(point["x_mm"]), float(point["y_mm"]))
        following = points[(index + 1) % len(points)]
        end = (float(following["x_mm"]), float(following["y_mm"]))
        if "arc_mid_x_mm" in point and "arc_mid_y_mm" in point:
            mid = (float(point["arc_mid_x_mm"]), float(point["arc_mid_y_mm"]))
            outline.edges.append(OutlineArc(start, mid, end))
        else:
            outline.edges.append(OutlineSegment(start, end))
    return outline


def register(mcp: FastMCP) -> None:
    """Register footprint locking and outline tools."""

    @mcp.tool()
    @requires_kicad_running
    def pcb_set_footprint_locked(reference: str, locked: bool = True) -> str:
        """Lock or unlock a footprint so interactive moves cannot (or can) drag it.

        The change is committed by the live KiCad over IPC and read back.
        """

        def apply(board: Board) -> str:
            matches = [
                fp for fp in board.get_footprints() if fp.reference_field.text.value == reference
            ]
            if not matches:
                return f"Footprint '{reference}' was not found on the active board."
            footprint = matches[0]
            footprint._proto.locked = LockedState.LS_LOCKED if locked else LockedState.LS_UNLOCKED
            board.update_items([cast(BoardItem, footprint)])
            again = [
                fp for fp in board.get_footprints() if fp.reference_field.text.value == reference
            ]
            state = again[0]._proto.locked == LockedState.LS_LOCKED if again else None
            if state is not locked:
                return f"Footprint '{reference}' lock could not be verified after the update."
            return f"Footprint '{reference}' is now {'locked' if locked else 'unlocked'}."

        return execute_live_board_mutation("pcb_set_footprint_locked", apply, verifier=None)

    @mcp.tool()
    @requires_kicad_running
    def pcb_set_board_outline_shape(points: list[dict[str, Any]], replace: bool = False) -> str:
        """Draw a closed board outline on Edge.Cuts from points, with optional arcs.

        ``points`` are ``{x_mm, y_mm}`` in order around the board; the last joins the
        first. Give ``arc_mid_x_mm`` and ``arc_mid_y_mm`` on a point to make the edge
        from it to the next point an arc through that middle point. An existing outline
        is kept unless ``replace`` is true.
        """
        try:
            outline = _outline_from_points(points)
        except (KeyError, TypeError, ValueError) as exc:
            return f"Board outline failed: {exc}"
        return execute_live_board_mutation(
            "pcb_set_board_outline_shape",
            lambda board: _draw(board, outline, replace),
            verifier=None,
        )

    @mcp.tool()
    @requires_kicad_running
    def pcb_import_outline_dxf(
        path: str,
        x_mm: float = 0.0,
        y_mm: float = 0.0,
        holes: str = "edge_cuts",
        replace: bool = False,
    ) -> str:
        """Draw a board outline and its holes from a DXF file (for example from an enclosure).

        Lines, arcs and polylines become the outline on Edge.Cuts; circles become holes
        (``holes="edge_cuts"``, cut-outs a board house drills as non-plated holes) or are
        reported only (``holes="none"``). Units follow the DXF header; the outline is
        placed with its top-left corner at (``x_mm``, ``y_mm``).
        """
        try:
            source = get_config().resolve_within_project(path)
            outline = read_dxf_outline(source)
        except (OSError, ValueError) as exc:
            return f"DXF import failed: {exc}"
        if not outline.edges:
            return "DXF import failed: the file has no lines, arcs or polylines for an outline."
        if holes not in ("edge_cuts", "none"):
            return "DXF import failed: holes must be edge_cuts or none."
        outline = placed(outline, (x_mm, y_mm))
        listed = [
            f"({h.center[0]:.2f}, {h.center[1]:.2f}) d={h.diameter:.2f}" for h in outline.holes
        ]
        if holes == "none":
            outline = DxfOutline(
                edges=outline.edges, skipped=outline.skipped, units_mm=outline.units_mm
            )
        result = execute_live_board_mutation(
            "pcb_import_outline_dxf", lambda board: _draw(board, outline, replace), verifier=None
        )
        extra = []
        if listed:
            extra.append("Holes in the DXF: " + "; ".join(listed) + ".")
        if outline.skipped:
            extra.append(
                "Skipped entities: "
                + ", ".join(f"{name} x{count}" for name, count in sorted(outline.skipped.items()))
                + "."
            )
        return "\n".join([result, *extra])


__all__ = ["OutlineHole", "register"]
