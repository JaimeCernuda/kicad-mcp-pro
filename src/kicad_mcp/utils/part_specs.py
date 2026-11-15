"""Normalized part specifications ("specs") reported by component sources.

Every distributor reports a part's attributes differently: Nexar sends
``specs[{attribute{name}, displayValue}]``, DigiKey ``Parameters[{ParameterText,
ValueText}]``, Mouser ``ProductAttributes[{AttributeName, AttributeValue}]`` and
jlcsearch's category endpoints typed columns (``capacitance_farads``,
``voltage_rating``) plus an ``attributes`` JSON string. This module turns each into
:class:`PartSpec` rows that keep the value as shown, its number in SI base units, the
unit and the source, so ``100nF`` and ``0.1µF`` compare equal.

Pure domain code: no network, no KiCad.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PartSpec:
    """One attribute of a part, as one source reported it."""

    #: Canonical name (``capacitance``, ``voltage_rating``, ...), or the source's
    #: name folded to ``snake_case`` when it has no canonical alias.
    name: str
    #: The value as the source showed it (``"10µF"``, ``"X7R"``, ``"±10%"``).
    display: str
    #: The value in SI base units (farads, volts, amps, ohms, ...), when numeric.
    number: float | None
    #: The base unit of ``number`` (``F``, ``V``, ``A``, ``Ω``, ``Hz``, ``W``, ``°C``,
    #: ``%``, ``dB``, ``s``, ``H``, ``K/W``), or ``""``.
    unit: str
    #: Which source reported it (``jlcsearch``, ``nexar``, ``digikey``, ``mouser``).
    source: str
    #: The source's own name for the attribute.
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready mapping of this spec."""
        return asdict(self)


_PREFIXES: dict[str, float] = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}

#: Unit spellings to their base unit.
_UNITS: dict[str, str] = {
    "f": "F",
    "farad": "F",
    "farads": "F",
    "v": "V",
    "vdc": "V",
    "vac": "V",
    "volt": "V",
    "volts": "V",
    "a": "A",
    "amp": "A",
    "amps": "A",
    "ampere": "A",
    "ω": "Ω",
    "Ω": "Ω",
    "ohm": "Ω",
    "ohms": "Ω",
    "r": "Ω",
    "hz": "Hz",
    "w": "W",
    "watt": "W",
    "watts": "W",
    "h": "H",
    "henry": "H",
    "s": "s",
    "sec": "s",
    "°c": "°C",
    "℃": "°C",
    "c": "°C",
    "%": "%",
    "db": "dB",
    "k/w": "K/W",
    "°c/w": "K/W",
    "℃/w": "K/W",
}

_QUANTITY = re.compile(
    r"^\s*(?P<sign>[±+-]?)\s*(?P<num>\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*(?P<rest>\S*)\s*$"
)

#: Source attribute names (folded) to canonical names.
_ALIASES: dict[str, str] = {
    "capacitance": "capacitance",
    "voltage_rating": "voltage_rating",
    "voltage_rated": "voltage_rating",
    "rated_voltage": "voltage_rating",
    "voltage_rated_dc": "voltage_rating",
    "voltage_-_rated": "voltage_rating",
    "voltage_rating_dc": "voltage_rating",
    "temperature_coefficient": "temperature_coefficient",
    "dielectric": "temperature_coefficient",
    "dielectric_characteristic": "temperature_coefficient",
    "tolerance": "tolerance",
    "resistance": "resistance",
    "inductance": "inductance",
    "power": "power_rating",
    "power_(watts)": "power_rating",
    "power_rating": "power_rating",
    "case/package": "package",
    "case_/_package": "package",
    "package_/_case": "package",
    "package": "package",
    "supplier_device_package": "package",
    "output_voltage": "output_voltage",
    "voltage_-_output_(min/fixed)": "output_voltage",
    "output_current": "output_current",
    "current_-_output": "output_current",
    "dropout_voltage": "dropout_voltage",
    "voltage_dropout_(max)": "dropout_voltage",
    "input_voltage_max": "input_voltage_max",
    "voltage_-_input_(max)": "input_voltage_max",
    "quiescent_current": "quiescent_current",
    "current_-_quiescent_(iq)": "quiescent_current",
    "operating_temperature": "operating_temperature",
    "thermal_resistance": "thermal_resistance",
    "lifecycle_status": "lifecycle",
    "rohs": "rohs",
    "esr": "esr",
}

#: jlcsearch typed columns that are not specs.
_JLC_SKIP = frozenset(
    {
        "lcsc",
        "mfr",
        "description",
        "stock",
        "price1",
        "price",
        "in_stock",
        "is_basic",
        "is_preferred",
        "attributes",
    }
)

#: jlcsearch column suffixes to (unit, scale): ``tolerance_fraction`` 0.1 is 10 %.
_JLC_SUFFIX_UNITS: tuple[tuple[str, str, float], ...] = (
    ("_farads", "F", 1.0),
    ("_ohms", "Ω", 1.0),
    ("_amps", "A", 1.0),
    ("_volts", "V", 1.0),
    ("_watts", "W", 1.0),
    ("_henries", "H", 1.0),
    ("_hz", "Hz", 1.0),
    ("_db", "dB", 1.0),
    ("_hours", "s", 3600.0),
    ("_fraction", "%", 100.0),
    ("_uvrms", "V", 1e-6),
)

#: jlcsearch column names whose unit is implied by the name.
_JLC_NAME_UNITS: tuple[tuple[str, str], ...] = (
    ("voltage", "V"),
    ("current", "A"),
    ("temp", "°C"),
    ("resistance", "Ω"),
    ("capacitance", "F"),
    ("frequency", "Hz"),
)


def fold_name(name: str) -> str:
    """Fold an attribute name to ``snake_case`` (spaces and dashes kept readable)."""
    folded = re.sub(r"\s+", "_", name.strip().casefold())
    return folded.strip("_")


def canonical_name(name: str) -> str:
    """The canonical spec name for a source's attribute name."""
    folded = fold_name(name)
    return _ALIASES.get(folded, folded)


def base_unit(unit: str) -> str:
    """The base unit for a unit spelling (``uF`` is not a unit: pass ``F``)."""
    if not unit:
        return ""
    return _UNITS.get(unit, _UNITS.get(unit.casefold(), unit))


def _unit_for(text: str) -> str | None:
    if text in _UNITS.values():
        return text
    return _UNITS.get(text.casefold())


def parse_quantity(text: str) -> tuple[float | None, str]:
    """Parse ``"10µF"``, ``"0.1 uF"``, ``"25V"``, ``"±10%"``, ``"4.7k"`` into (SI number, unit).

    Text that is not one quantity (``"X7R"``, ``"10pcs"``) returns ``(None, "")``; a
    bare number returns it with no unit.
    """
    match = _QUANTITY.match(str(text))
    if match is None:
        return None, ""
    number = float(match.group("num").replace(",", "."))
    if match.group("sign") == "-":
        number = -number
    rest = match.group("rest")
    if not rest:
        return number, ""
    if (unit := _unit_for(rest)) is not None:
        return number, unit
    prefix, tail = rest[0], rest[1:]
    if prefix in _PREFIXES and prefix:
        if not tail:
            return number * _PREFIXES[prefix], ""
        if (unit := _unit_for(tail)) is not None:
            return number * _PREFIXES[prefix], unit
    return None, ""


def spec(
    name: str, display: object, source: str, *, number: float | None = None, unit: str = ""
) -> PartSpec:
    """One :class:`PartSpec` from a source's name and display value."""
    shown = "" if display is None else str(display).strip()
    if number is None:
        number, parsed_unit = parse_quantity(shown)
        unit = unit or parsed_unit
    return PartSpec(
        name=canonical_name(name),
        display=shown,
        number=number,
        unit=base_unit(unit),
        source=source,
        label=str(name).strip(),
    )


def specs_from_pairs(pairs: Iterable[tuple[Any, Any]], source: str) -> tuple[PartSpec, ...]:
    """Specs from (name, display) pairs, skipping empty names and values."""
    rows: list[PartSpec] = []
    for name, display in pairs:
        if not str(name or "").strip() or display in (None, "", "-"):
            continue
        rows.append(spec(str(name), display, source))
    return tuple(rows)


def specs_from_nexar(specs: Iterable[Mapping[str, Any]] | None) -> tuple[PartSpec, ...]:
    """Nexar ``specs[{attribute{name}, displayValue}]``."""
    return specs_from_pairs(
        (
            ((item.get("attribute") or {}).get("name"), item.get("displayValue"))
            for item in specs or ()
        ),
        "nexar",
    )


def specs_from_digikey(parameters: Iterable[Mapping[str, Any]] | None) -> tuple[PartSpec, ...]:
    """DigiKey v4 ``Parameters[{ParameterText, ValueText}]``."""
    return specs_from_pairs(
        ((item.get("ParameterText"), item.get("ValueText")) for item in parameters or ()),
        "digikey",
    )


def specs_from_mouser(attributes: Iterable[Mapping[str, Any]] | None) -> tuple[PartSpec, ...]:
    """Mouser ``ProductAttributes[{AttributeName, AttributeValue}]``."""
    return specs_from_pairs(
        ((item.get("AttributeName"), item.get("AttributeValue")) for item in attributes or ()),
        "mouser",
    )


def _jlc_column_unit(column: str) -> tuple[str, str, float]:
    for suffix, unit, scale in _JLC_SUFFIX_UNITS:
        if column.endswith(suffix):
            return column[: -len(suffix)], unit, scale
    for needle, unit in _JLC_NAME_UNITS:
        if needle in column:
            return column, unit, 1.0
    return column, "", 1.0


def _display_number(number: float, unit: str) -> str:
    if unit in ("", "%", "dB", "°C", "K/W", "s"):
        return f"{number:g}{unit}"
    for prefix, scale in (
        ("G", 1e9),
        ("M", 1e6),
        ("k", 1e3),
        ("", 1.0),
        ("m", 1e-3),
        ("µ", 1e-6),
        ("n", 1e-9),
        ("p", 1e-12),
    ):
        if abs(number) >= scale * 0.9999 or prefix == "p":
            return f"{number / scale:.4g}{prefix}{unit}"
    return f"{number:g}{unit}"


def specs_from_jlcsearch(item: Mapping[str, Any]) -> tuple[PartSpec, ...]:
    """A jlcsearch category row: its typed columns, then its ``attributes`` text.

    A typed column wins over the same attribute in ``attributes``, because it is
    already a number in a known unit.
    """
    rows: dict[str, PartSpec] = {}
    for column, value in item.items():
        if column in _JLC_SKIP or value is None or value == "":
            continue
        name, unit, scale = _jlc_column_unit(column)
        canonical = canonical_name(name)
        if isinstance(value, bool):
            rows[canonical] = PartSpec(
                canonical, "yes" if value else "no", None, "", "jlcsearch", column
            )
        elif isinstance(value, (int, float)):
            number = float(value) * scale
            rows[canonical] = PartSpec(
                canonical, _display_number(number, unit), number, unit, "jlcsearch", column
            )
        else:
            rows[canonical] = spec(column, value, "jlcsearch")
    attributes = item.get("attributes")
    if isinstance(attributes, str) and attributes.strip():
        try:
            parsed = json.loads(attributes)
        except ValueError:
            parsed = {}
        if isinstance(parsed, Mapping):
            for found in specs_from_pairs(parsed.items(), "jlcsearch"):
                rows.setdefault(found.name, found)
    return tuple(rows.values())


_DESCRIPTION_TOKEN = re.compile(
    r"[±+-]?\d+(?:\.\d+)?\s*[pnuµμmkKMG]?(?:F|V|A|Ω|W|H|Hz|%|ohm)(?![A-Za-z])"
)
_DIELECTRICS = re.compile(r"\b(C0G|NP0|X5R|X6S|X7R|X7S|X8R|Y5V|Z5U)\b")


def specs_from_description(description: str, source: str) -> tuple[PartSpec, ...]:
    """Best-effort specs read from a one-line description (``10uF 25V X7R 0805``).

    These rows carry ``source`` with a ``:description`` suffix so a reader can tell
    a parsed description from an attribute the source reported.
    """
    label = f"{source}:description"
    rows: dict[str, PartSpec] = {}
    unit_names = {
        "F": "capacitance",
        "V": "voltage_rating",
        "Ω": "resistance",
        "H": "inductance",
        "W": "power_rating",
        "%": "tolerance",
        "A": "current_rating",
    }
    for token in _DESCRIPTION_TOKEN.findall(description or ""):
        number, unit = parse_quantity(token.replace("ohm", "Ω"))
        name = unit_names.get(unit)
        if name and name not in rows:
            rows[name] = PartSpec(name, token.strip(), number, unit, label, "description")
    if match := _DIELECTRICS.search(description or ""):
        rows.setdefault(
            "temperature_coefficient",
            PartSpec("temperature_coefficient", match.group(1), None, "", label, "description"),
        )
    return tuple(rows.values())


# ----------------------------------------------------------------- filters

OPERATORS = ("=", "!=", ">=", "<=", ">", "<", "contains")


@dataclass(frozen=True)
class SpecFilter:
    """``{name, op, value, unit}``: one condition on a spec."""

    name: str
    op: str
    value: str
    unit: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SpecFilter:
        """Validate one filter mapping; raises ``ValueError`` with what is wrong."""
        name = canonical_name(str(raw.get("name", "")))
        op = str(raw.get("op", "=")).strip() or "="
        if not name:
            raise ValueError("a filter needs a name")
        if op not in OPERATORS:
            raise ValueError(f"filter {name!r}: op must be one of {', '.join(OPERATORS)}")
        value = raw.get("value", "")
        if value in (None, ""):
            raise ValueError(f"filter {name!r}: value is empty")
        return cls(
            name=name, op=op, value=str(value).strip(), unit=str(raw.get("unit") or "").strip()
        )

    def target(self) -> tuple[float | None, str]:
        """The filter value as (SI number, unit): ``25`` with unit ``V``, or ``"10uF"``."""
        if self.unit:
            number, unit = parse_quantity(f"{self.value}{self.unit}")
            if number is None:
                number, _ = parse_quantity(self.value)
                unit = base_unit(self.unit)
            return number, unit
        return parse_quantity(self.value)

    def matches(self, specs: Iterable[PartSpec]) -> bool:
        """Whether any spec of this name meets the condition."""
        number, unit = self.target()
        for found in specs:
            if found.name != self.name:
                continue
            if self.op == "contains":
                if self.value.casefold() in found.display.casefold():
                    return True
                continue
            if (
                number is not None
                and found.number is not None
                and (not unit or not found.unit or unit == found.unit)
            ):
                if _compare(found.number, self.op, number):
                    return True
                continue
            equal = found.display.casefold() == self.value.casefold()
            if (self.op == "=" and equal) or (self.op == "!=" and not equal):
                return True
        return False


def _compare(left: float, op: str, right: float) -> bool:
    tolerance = max(abs(right), abs(left), 1e-30) * 1e-6
    if op == "=":
        return abs(left - right) <= tolerance
    if op == "!=":
        return abs(left - right) > tolerance
    if op == ">=":
        return left >= right - tolerance
    if op == "<=":
        return left <= right + tolerance
    if op == ">":
        return left > right + tolerance
    if op == "<":
        return left < right - tolerance
    return False


def merge_specs(groups: Iterable[Iterable[PartSpec]]) -> tuple[PartSpec, ...]:
    """Every spec of every source, ordered by name; nothing is dropped.

    Two sources reporting the same attribute both stay, each with its source, so a
    reader sees agreement or disagreement.
    """
    seen: set[tuple[str, str, str]] = set()
    rows: list[PartSpec] = []
    for group in groups:
        for found in group:
            key = (found.name, found.source, found.display)
            if key in seen:
                continue
            seen.add(key)
            rows.append(found)
    return tuple(sorted(rows, key=lambda row: (row.name, row.source)))


def first_number(specs: Iterable[PartSpec], name: str) -> PartSpec | None:
    """The first numeric spec of ``name``: a reported attribute before a parsed description."""
    candidates = [
        found for found in specs if found.name == canonical_name(name) and found.number is not None
    ]
    candidates.sort(key=lambda found: found.source.endswith(":description"))
    return candidates[0] if candidates else None
