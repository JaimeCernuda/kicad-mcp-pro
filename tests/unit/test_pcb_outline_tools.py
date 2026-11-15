"""Board outlines from points or DXF, and footprint locking."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kipy.board_types import BoardArc, BoardCircle, BoardSegment
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types.base_types_pb2 import LockedState
from mcp.server.mcpserver import MCPServer as FastMCP

from kicad_mcp.pcb.dxf_outline import OutlineArc, OutlineSegment, placed, read_dxf_outline
from kicad_mcp.tools import pcb_outline_tools


def _write_dxf(path: Path, *, units: int = 4) -> Path:
    width, height, radius = 60.0, 40.0, 3.0
    bulge = math.tan(math.radians(90) / 4)
    points = [
        (radius, 0, 0),
        (width - radius, 0, bulge),
        (width, radius, 0),
        (width, height - radius, bulge),
        (width - radius, height, 0),
        (radius, height, bulge),
        (0, height - radius, 0),
        (0, radius, bulge),
    ]
    lines = ["0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", str(units), "0", "ENDSEC"]
    lines += ["0", "SECTION", "2", "ENTITIES", "0", "LWPOLYLINE", "90", "8", "70", "1"]
    for x, y, b in points:
        lines += ["10", str(x), "20", str(y)]
        if b:
            lines += ["42", str(b)]
    for x, y in [(4, 4), (56, 4), (56, 36), (4, 36)]:
        lines += ["0", "CIRCLE", "10", str(x), "20", str(y), "40", "1.6"]
    lines += ["0", "TEXT", "1", "label", "0", "ENDSEC", "0", "EOF"]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_dxf_outline_reads_polyline_arcs_holes_and_flips_y(tmp_path: Path) -> None:
    outline = read_dxf_outline(_write_dxf(tmp_path / "board.dxf"))
    assert len(outline.edges) == 8
    assert sum(isinstance(edge, OutlineArc) for edge in outline.edges) == 4
    assert len(outline.holes) == 4 and outline.holes[0].diameter == pytest.approx(3.2)
    assert outline.skipped == {"TEXT": 1}
    min_x, min_y, max_x, max_y = outline.bounds() or (0, 0, 0, 0)
    assert (max_x - min_x, max_y - min_y) == (pytest.approx(60.0), pytest.approx(40.0))
    assert max_y <= 0  # DXF Y up became KiCad Y down
    corner = next(edge for edge in outline.edges if isinstance(edge, OutlineArc))
    # The rounded corner bulges outward: its middle is outside the chord.
    assert abs(corner.mid[0] - (corner.start[0] + corner.end[0]) / 2) > 0.5


def test_dxf_inches_are_converted(tmp_path: Path) -> None:
    outline = read_dxf_outline(_write_dxf(tmp_path / "board.dxf", units=1))
    min_x, _min_y, max_x, _max_y = outline.bounds() or (0, 0, 0, 0)
    assert max_x - min_x == pytest.approx(60.0 * 25.4)


def test_placed_moves_the_top_left_corner(tmp_path: Path) -> None:
    outline = placed(read_dxf_outline(_write_dxf(tmp_path / "board.dxf")), (100.0, 50.0))
    min_x, min_y, _max_x, _max_y = outline.bounds() or (0, 0, 0, 0)
    assert (min_x, min_y) == (pytest.approx(100.0), pytest.approx(50.0))


def test_not_a_dxf_is_a_clear_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.dxf"
    bad.write_text("hello\nworld\n")
    with pytest.raises(ValueError, match="Not an ASCII DXF"):
        read_dxf_outline(bad)


class _Board:
    def __init__(
        self, shapes: list[Any] | None = None, footprints: list[Any] | None = None
    ) -> None:
        self.shapes = shapes or []
        self.footprints = footprints or []
        self.created: list[Any] = []
        self.removed: list[Any] = []
        self.updated: list[Any] = []

    def get_shapes(self) -> list[Any]:
        return self.shapes

    def remove_items(self, items: list[Any]) -> None:
        self.removed.extend(items)

    def create_items(self, items: list[Any]) -> list[Any]:
        self.created.extend(items)
        return items

    def get_footprints(self) -> list[Any]:
        return self.footprints

    def update_items(self, items: list[Any]) -> list[Any]:
        self.updated.extend(items)
        return items


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> tuple[FastMCP, dict[str, _Board]]:
    holder: dict[str, _Board] = {"board": _Board()}
    monkeypatch.setattr(
        pcb_outline_tools,
        "execute_live_board_mutation",
        lambda operation, command, verifier: command(holder["board"]),
    )
    server = FastMCP("x")
    pcb_outline_tools.register(server)
    return server, holder


def _call(server: FastMCP, name: str, **kwargs: object) -> str:
    return server._tool_manager.get_tool(name).fn(**kwargs)


def test_outline_from_points_draws_segments_and_arcs(tools) -> None:
    server, holder = tools
    text = _call(
        server,
        "pcb_set_board_outline_shape",
        points=[
            {"x_mm": 0, "y_mm": 0},
            {"x_mm": 50, "y_mm": 0, "arc_mid_x_mm": 55, "arc_mid_y_mm": 15},
            {"x_mm": 50, "y_mm": 30},
            {"x_mm": 0, "y_mm": 30},
        ],
    )
    assert "4 edges and 0 holes" in text
    kinds = [type(item) for item in holder["board"].created]
    assert kinds.count(BoardArc) == 1 and kinds.count(BoardSegment) == 3
    assert all(item.layer == BoardLayer.BL_Edge_Cuts for item in holder["board"].created)


def test_outline_needs_three_points_and_keeps_an_existing_outline(tools) -> None:
    server, holder = tools
    assert "at least three points" in _call(
        server, "pcb_set_board_outline_shape", points=[{"x_mm": 0, "y_mm": 0}]
    )
    holder["board"] = _Board(shapes=[SimpleNamespace(layer=BoardLayer.BL_Edge_Cuts)])
    square = [{"x_mm": 0, "y_mm": 0}, {"x_mm": 1, "y_mm": 0}, {"x_mm": 1, "y_mm": 1}]
    assert "replace=true" in _call(server, "pcb_set_board_outline_shape", points=square)
    assert not holder["board"].created
    _call(server, "pcb_set_board_outline_shape", points=square, replace=True)
    assert len(holder["board"].removed) == 1 and len(holder["board"].created) == 3


def test_dxf_import_draws_outline_and_hole_cutouts(tools, monkeypatch, tmp_path: Path) -> None:
    server, holder = tools
    dxf = _write_dxf(tmp_path / "enclosure.dxf")
    monkeypatch.setattr(
        pcb_outline_tools,
        "get_config",
        lambda: SimpleNamespace(resolve_within_project=lambda path: tmp_path / path),
    )
    text = _call(server, "pcb_import_outline_dxf", path=dxf.name, x_mm=100, y_mm=50)
    assert "8 edges and 4 holes" in text and "60.00 x 40.00 mm" in text
    assert "Skipped entities: TEXT x1." in text
    assert sum(isinstance(item, BoardCircle) for item in holder["board"].created) == 4
    holder["board"] = _Board()
    text = _call(server, "pcb_import_outline_dxf", path=dxf.name, holes="none")
    assert "0 holes" in text and "Holes in the DXF" in text
    assert "failed" in _call(server, "pcb_import_outline_dxf", path="missing.dxf")


def test_lock_and_unlock_a_footprint(tools) -> None:
    server, holder = tools
    footprint = SimpleNamespace(
        reference_field=SimpleNamespace(text=SimpleNamespace(value="U1")),
        _proto=SimpleNamespace(locked=LockedState.LS_UNLOCKED),
    )
    holder["board"] = _Board(footprints=[footprint])
    assert (
        _call(server, "pcb_set_footprint_locked", reference="U1") == "Footprint 'U1' is now locked."
    )
    assert footprint._proto.locked == LockedState.LS_LOCKED
    assert "unlocked" in _call(server, "pcb_set_footprint_locked", reference="U1", locked=False)
    assert "not found" in _call(server, "pcb_set_footprint_locked", reference="J9")


def test_segments_are_straight_edges() -> None:
    assert OutlineSegment((0, 0), (1, 0)).end == (1, 0)
