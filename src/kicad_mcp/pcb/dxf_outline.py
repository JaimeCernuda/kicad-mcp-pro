"""Read a board outline and hole positions from an ASCII DXF file.

Pure domain code: it parses the DXF group-code stream and returns geometry in
millimetres with KiCad's axes (Y down). Supported entities: ``LINE``, ``ARC``,
``CIRCLE``, ``LWPOLYLINE`` (with bulges) and R12 ``POLYLINE``/``VERTEX``. Circles are
returned as holes; everything else forms the outline. Units come from the header's
``$INSUNITS`` (inches, millimetres, centimetres, metres) and default to millimetres.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

#: ``$INSUNITS`` codes to millimetres.
_UNITS_MM: dict[int, float] = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}


@dataclass(frozen=True)
class OutlineSegment:
    """A straight edge, in millimetres."""

    start: tuple[float, float]
    end: tuple[float, float]


@dataclass(frozen=True)
class OutlineArc:
    """An arc through three points, in millimetres."""

    start: tuple[float, float]
    mid: tuple[float, float]
    end: tuple[float, float]


@dataclass(frozen=True)
class OutlineHole:
    """A round hole: its centre and diameter, in millimetres."""

    center: tuple[float, float]
    diameter: float


@dataclass
class DxfOutline:
    """Everything read from the file."""

    edges: list[OutlineSegment | OutlineArc] = field(default_factory=list)
    holes: list[OutlineHole] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    units_mm: float = 1.0

    def bounds(self) -> tuple[float, float, float, float] | None:
        """(min x, min y, max x, max y) of the outline, or ``None`` when empty."""
        points = [p for edge in self.edges for p in _points(edge)]
        if not points:
            return None
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return min(xs), min(ys), max(xs), max(ys)


def _points(edge: OutlineSegment | OutlineArc) -> list[tuple[float, float]]:
    if isinstance(edge, OutlineArc):
        return [edge.start, edge.mid, edge.end]
    return [edge.start, edge.end]


def _pairs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    if len(lines) % 2:
        lines = lines[:-1]
    pairs: list[tuple[int, str]] = []
    for index in range(0, len(lines), 2):
        try:
            pairs.append((int(lines[index].strip()), lines[index + 1].strip()))
        except ValueError as exc:
            raise ValueError(f"Not an ASCII DXF file (line {index + 1}).") from exc
    return pairs


def _entities(pairs: list[tuple[int, str]]) -> list[tuple[str, list[tuple[int, str]]]]:
    """The ENTITIES section as (type, group codes) records."""
    out: list[tuple[str, list[tuple[int, str]]]] = []
    in_entities = False
    current: tuple[str, list[tuple[int, str]]] | None = None
    for index, (code, value) in enumerate(pairs):
        if code == 2 and index and pairs[index - 1] == (0, "SECTION"):
            in_entities = value == "ENTITIES"
            continue
        if not in_entities:
            continue
        if code == 0:
            if current is not None:
                out.append(current)
            if value == "ENDSEC":
                in_entities, current = False, None
                continue
            current = (value, [])
        elif current is not None:
            current[1].append((code, value))
    if current is not None:
        out.append(current)
    return out


def _header_units(pairs: list[tuple[int, str]]) -> float:
    for index, (code, value) in enumerate(pairs):
        if code == 9 and value == "$INSUNITS" and index + 1 < len(pairs):
            try:
                return _UNITS_MM.get(int(pairs[index + 1][1]), 1.0)
            except ValueError:
                return 1.0
    return 1.0


def _first(codes: list[tuple[int, str]], code: int, default: float = 0.0) -> float:
    for found, value in codes:
        if found == code:
            return float(value)
    return default


def _arc_points(
    cx: float, cy: float, r: float, start_deg: float, end_deg: float
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """Start, mid and end of a counter-clockwise DXF arc (Y up)."""
    if end_deg < start_deg:
        end_deg += 360.0
    mid_deg = (start_deg + end_deg) / 2.0

    def at(deg: float) -> tuple[float, float]:
        rad = math.radians(deg)
        return cx + r * math.cos(rad), cy + r * math.sin(rad)

    return at(start_deg), at(mid_deg), at(end_deg)


def _bulge_mid(
    p1: tuple[float, float], p2: tuple[float, float], bulge: float
) -> tuple[float, float]:
    """The middle of a polyline arc segment with ``bulge`` (Y up)."""
    mx, my = (p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    return mx + dy * bulge / 2.0, my - dx * bulge / 2.0


def _polyline(
    vertices: list[tuple[float, float, float]], closed: bool
) -> list[OutlineSegment | OutlineArc]:
    edges: list[OutlineSegment | OutlineArc] = []
    count = len(vertices)
    limit = count if closed else count - 1
    for index in range(limit):
        x1, y1, bulge = vertices[index]
        x2, y2, _ = vertices[(index + 1) % count]
        if abs(bulge) > 1e-12:
            edges.append(OutlineArc((x1, y1), _bulge_mid((x1, y1), (x2, y2), bulge), (x2, y2)))
        else:
            edges.append(OutlineSegment((x1, y1), (x2, y2)))
    return edges


def _lwpolyline(codes: list[tuple[int, str]]) -> list[OutlineSegment | OutlineArc]:
    closed = int(_first(codes, 70, 0)) & 1 == 1
    vertices: list[tuple[float, float, float]] = []
    x: float | None = None
    for code, value in codes:
        if code == 10:
            x = float(value)
        elif code == 20 and x is not None:
            vertices.append((x, float(value), 0.0))
            x = None
        elif code == 42 and vertices:
            vx, vy, _ = vertices[-1]
            vertices[-1] = (vx, vy, float(value))
    return _polyline(vertices, closed)


def read_dxf_outline(path: Path) -> DxfOutline:
    """Read ``path`` into an outline and holes, in millimetres with Y down."""
    text = path.read_text(encoding="utf-8", errors="replace")
    pairs = _pairs(text)
    scale = _header_units(pairs)
    result = DxfOutline(units_mm=scale)
    raw: list[OutlineSegment | OutlineArc] = []
    entities = _entities(pairs)
    index = 0
    while index < len(entities):
        kind, codes = entities[index]
        index += 1
        if kind == "LINE":
            raw.append(
                OutlineSegment(
                    (_first(codes, 10), _first(codes, 20)), (_first(codes, 11), _first(codes, 21))
                )
            )
        elif kind == "ARC":
            raw.append(
                OutlineArc(
                    *_arc_points(
                        _first(codes, 10),
                        _first(codes, 20),
                        _first(codes, 40),
                        _first(codes, 50),
                        _first(codes, 51),
                    )
                )
            )
        elif kind == "CIRCLE":
            result.holes.append(
                OutlineHole((_first(codes, 10), _first(codes, 20)), 2.0 * _first(codes, 40))
            )
        elif kind == "LWPOLYLINE":
            raw.extend(_lwpolyline(codes))
        elif kind == "POLYLINE":
            closed = int(_first(codes, 70, 0)) & 1 == 1
            vertices: list[tuple[float, float, float]] = []
            while index < len(entities) and entities[index][0] == "VERTEX":
                vcodes = entities[index][1]
                vertices.append((_first(vcodes, 10), _first(vcodes, 20), _first(vcodes, 42)))
                index += 1
            if index < len(entities) and entities[index][0] == "SEQEND":
                index += 1
            raw.extend(_polyline(vertices, closed))
        else:
            result.skipped[kind] = result.skipped.get(kind, 0) + 1

    def tr(point: tuple[float, float]) -> tuple[float, float]:
        return point[0] * scale, -point[1] * scale

    for edge in raw:
        if isinstance(edge, OutlineArc):
            result.edges.append(OutlineArc(tr(edge.start), tr(edge.mid), tr(edge.end)))
        else:
            result.edges.append(OutlineSegment(tr(edge.start), tr(edge.end)))
    result.holes = [OutlineHole(tr(hole.center), hole.diameter * scale) for hole in result.holes]
    return result


def placed(outline: DxfOutline, at: tuple[float, float]) -> DxfOutline:
    """The outline moved so its top-left corner is at ``at`` (millimetres)."""
    bounds = outline.bounds()
    if bounds is None:
        return outline
    dx, dy = at[0] - bounds[0], at[1] - bounds[1]

    def mv(point: tuple[float, float]) -> tuple[float, float]:
        return point[0] + dx, point[1] + dy

    moved = DxfOutline(skipped=dict(outline.skipped), units_mm=outline.units_mm)
    for edge in outline.edges:
        if isinstance(edge, OutlineArc):
            moved.edges.append(OutlineArc(mv(edge.start), mv(edge.mid), mv(edge.end)))
        else:
            moved.edges.append(OutlineSegment(mv(edge.start), mv(edge.end)))
    moved.holes = [OutlineHole(mv(hole.center), hole.diameter) for hole in outline.holes]
    return moved
