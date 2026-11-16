"""Map live KiCad pads to their containing footprint instances."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class MappedPad:
    """One authoritative board pad enriched with its footprint reference."""

    pad: object
    pad_id: str | None
    reference: str | None


def pad_id(pad: object) -> str | None:
    """Return a stable KiCad pad identifier when the IPC object exposes one."""
    value = getattr(getattr(pad, "id", None), "value", None)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def footprint_reference(footprint: object) -> str | None:
    """Return a placed footprint reference through the supported field surface."""
    value = getattr(
        getattr(getattr(footprint, "reference_field", None), "text", None),
        "value",
        None,
    )
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def footprint_pads(footprint: object) -> list[object]:
    """Read pads from the official ``FootprintInstance.definition.pads`` relationship."""
    pads = getattr(getattr(footprint, "definition", None), "pads", None)
    if pads is None:
        return []
    try:
        return list(pads)
    except TypeError:
        return []


def _footprint_pad_references(footprints: Iterable[object]) -> dict[str, str | None]:
    """Index the footprint reference for each official pad identifier."""
    references: dict[str, str | None] = {}
    for footprint in footprints:
        reference = footprint_reference(footprint)
        for pad in footprint_pads(footprint):
            identifier = pad_id(pad)
            if identifier is None:
                continue
            if identifier not in references:
                references[identifier] = reference
            elif references[identifier] != reference:
                references[identifier] = None

    return references


def map_pads_to_footprints(
    pads: Iterable[object],
    footprints: Iterable[object],
) -> list[MappedPad]:
    """Deduplicate board pads and enrich them with stable footprint references."""
    references = _footprint_pad_references(footprints)

    mapped: list[MappedPad] = []
    seen: set[tuple[str, str | int]] = set()
    for pad in pads:
        identifier = pad_id(pad)
        identity: tuple[str, str | int] = (
            ("id", identifier) if identifier is not None else ("object", id(pad))
        )
        if identity in seen:
            continue
        seen.add(identity)
        mapped.append(
            MappedPad(
                pad=pad,
                pad_id=identifier,
                reference=references.get(identifier) if identifier is not None else None,
            )
        )
    return mapped


def pad_references(pads: Iterable[object], footprints: Iterable[object]) -> dict[str, str]:
    """Each pad's footprint reference by pad ID (a KiCad 10 pad names its footprint by ID)."""
    return {
        item.pad_id: item.reference
        for item in map_pads_to_footprints(pads, footprints)
        if item.pad_id is not None and item.reference is not None
    }


def reference_of(pad: object, references: Mapping[str, str] | None = None) -> str | None:
    """The reference of the footprint ``pad`` belongs to: from ``references`` (built with
    ``pad_references``), else from a parent that is the footprint itself."""
    identifier = pad_id(pad)
    if references is not None and identifier is not None and identifier in references:
        return references[identifier]
    return footprint_reference(getattr(pad, "parent", None))
