"""Footprint transform helpers shared by live PCB mutation tools."""

from __future__ import annotations

import math
from typing import Any, Literal

from google.protobuf.descriptor import FieldDescriptor
from kipy.board_types import (
    BoardPolygon,
    BoardRectangle,
    BoardShape,
    BoardText,
    Field,
    Footprint3DModel,
    FootprintInstance,
    Group,
    Pad,
    Zone,
    to_concrete_board_shape,
)
from kipy.geometry import Angle, Vector2

RotationAttribute = Literal["angle", "orientation"]
_SUPPORTED_ROTATION_ATTRIBUTES: tuple[RotationAttribute, ...] = ("angle", "orientation")
_VECTOR2 = "kiapi.common.types.Vector2"


class FootprintRotationError(RuntimeError):
    """Raised when a requested footprint rotation cannot be applied or verified."""


def _kiround(value: float) -> int:
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


class _Rotation:
    """KiCad's RotatePoint: exact for multiples of 90 degrees, rounded to nearest otherwise.

    kicad-python's ``Vector2.rotate`` truncates with ``int()``, which leaves graphics 1 nm off
    the library footprint after a quarter turn.
    """

    def __init__(self, degrees: float, center: Vector2) -> None:
        quarter = round(degrees / 90.0)
        self.cardinal = math.isclose(degrees, 90.0 * quarter, abs_tol=1e-9)
        if self.cardinal:
            self.cos, self.sin = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[quarter % 4]
        else:
            self.cos, self.sin = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        self.cx, self.cy = center.x, center.y

    def xy(self, x: int, y: int) -> tuple[int, int]:
        dx, dy = x - self.cx, y - self.cy
        return (
            _kiround(dy * self.sin + dx * self.cos) + self.cx,
            _kiround(dy * self.cos - dx * self.sin) + self.cy,
        )

    def point(self, value: Vector2) -> Vector2:
        return Vector2.from_xy(*self.xy(value.x, value.y))

    def geometry(self, message: Any) -> None:  # noqa: ANN401 - any kiapi geometry message
        """Rotate every point of a shape or polygon message; each Vector2 in one is a position."""
        for field, value in message.ListFields():
            if field.message_type is None:
                continue
            for item in value if field.label == FieldDescriptor.LABEL_REPEATED else [value]:
                if field.message_type.full_name == _VECTOR2:
                    item.x_nm, item.y_nm = self.xy(item.x_nm, item.y_nm)
                else:
                    self.geometry(item)


def _rotate_footprint_instance(footprint: FootprintInstance, requested: Angle) -> None:
    """Rotate a footprint and its children in place, keeping every child and its order."""
    requested.normalize180()
    delta = requested - footprint.orientation
    items = list(footprint.definition.items)
    unsupported = sorted(
        {
            type(item).__name__
            for item in items
            if not isinstance(
                item, Field | Pad | BoardText | Zone | BoardShape | Footprint3DModel | Group
            )
        }
    )
    if unsupported:
        raise FootprintRotationError(
            f"Footprint contains items CLIO cannot rotate yet: {', '.join(unsupported)}."
        )
    rotation = _Rotation(delta.degrees, footprint.position)
    footprint.proto.orientation.CopyFrom(requested.proto)
    fields = [
        footprint.reference_field,
        footprint.value_field,
        footprint.datasheet_field,
        footprint.description_field,
    ]
    for field in [*fields, *(item for item in items if isinstance(item, Field))]:
        field.text.position = rotation.point(field.text.position)
        field.text.attributes.angle += delta.degrees
    for index, item in enumerate(items):
        if isinstance(item, Pad):
            item.position = rotation.point(item.position)
            item.padstack.angle += delta
        elif isinstance(item, BoardText):
            item.position = rotation.point(item.position)
            item.attributes.angle += delta.degrees
        elif isinstance(item, Zone):
            rotation.geometry(item.proto.outline)
            for filled in item.proto.filled_polygons:
                rotation.geometry(filled.shapes)
        elif isinstance(item, BoardShape):
            shape = to_concrete_board_shape(item)
            if isinstance(shape, BoardRectangle) and not rotation.cardinal:
                shape = items[index] = BoardPolygon.from_rectangle(shape)
            rotation.geometry(shape.proto.shape)
    footprint.definition.items = items


def _rotation_degrees(value: object) -> float:
    if isinstance(value, Angle):
        return float(value.degrees)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    degrees = getattr(value, "degrees", None)
    if isinstance(degrees, int | float) and not isinstance(degrees, bool):
        return float(degrees)
    raise FootprintRotationError("Footprint rotation value does not expose numeric degrees.")


def get_current_footprint_rotation(footprint: object) -> tuple[RotationAttribute, float]:
    """Read the existing writable rotation surface without assigning a new angle."""
    failures: list[str] = []
    available: list[RotationAttribute] = []
    for attribute in _SUPPORTED_ROTATION_ATTRIBUTES:
        try:
            value = getattr(footprint, attribute)
        except AttributeError:
            continue
        available.append(attribute)
        try:
            return attribute, _rotation_degrees(value)
        except FootprintRotationError as exc:
            failures.append(f"{attribute}: {exc}")
    if not available:
        raise FootprintRotationError(
            "Footprint does not expose a supported angle or orientation surface."
        )
    detail = ", ".join(failures) if failures else ", ".join(available)
    raise FootprintRotationError(f"Current footprint rotation could not be read ({detail}).")


def apply_footprint_rotation(footprint: object, rotation_deg: float) -> RotationAttribute:
    """Assign a KiCad ``Angle`` through the first writable rotation surface."""
    if not math.isfinite(rotation_deg):
        raise FootprintRotationError("Requested footprint rotation must be finite.")

    requested = Angle.from_degrees(rotation_deg)
    available: list[RotationAttribute] = []
    failures: list[str] = []
    for attribute in _SUPPORTED_ROTATION_ATTRIBUTES:
        try:
            getattr(footprint, attribute)
        except AttributeError:
            continue
        available.append(attribute)
        try:
            current_deg = _rotation_degrees(getattr(footprint, attribute))
        except FootprintRotationError:
            current_deg = None
        if current_deg is not None and abs((current_deg - rotation_deg + 180) % 360 - 180) < 1e-9:
            return attribute
        if isinstance(footprint, FootprintInstance) and attribute == "orientation":
            # kicad-python's own orientation setter truncates every rotated point and
            # rebuilds the children without 3D models, text boxes or dimensions.
            _rotate_footprint_instance(footprint, requested)
            return attribute
        try:
            setattr(footprint, attribute, requested)
        except Exception as exc:  # KiCad IPC surfaces raise generated transport/type errors.
            failures.append(f"{attribute}: {type(exc).__name__}")
            continue
        return attribute

    if not available:
        raise FootprintRotationError(
            "Footprint does not expose a supported angle or orientation surface."
        )
    detail = ", ".join(failures) if failures else ", ".join(available)
    raise FootprintRotationError(f"Requested footprint rotation could not be applied ({detail}).")


def verify_footprint_rotation(
    footprint: object,
    attribute: RotationAttribute,
    requested_deg: float,
    *,
    tolerance_deg: float = 1e-3,
) -> float:
    """Read back and verify a footprint rotation using wrapped angular distance."""
    try:
        actual = _rotation_degrees(getattr(footprint, attribute))
    except AttributeError as exc:
        raise FootprintRotationError(
            f"Footprint rotation verification failed: '{attribute}' is unavailable."
        ) from exc

    delta = (actual - requested_deg + 180.0) % 360.0 - 180.0
    if abs(delta) > tolerance_deg:
        raise FootprintRotationError(
            "Footprint rotation verification failed: "
            f"requested {requested_deg:.6f} degrees, observed {actual:.6f} degrees."
        )
    return actual
