"""Parametric part search, spec sheets and side-by-side comparison across sources.

Built on :mod:`kicad_mcp.utils.part_specs` (the normalized ``specs`` every
:class:`~kicad_mcp.utils.component_search.ComponentRecord` carries) and the existing
search clients. The service never raises for a source that fails: every answer lists
the sources that answered, the ones that failed and why, and, when no source with a
key is configured, which ones could be and by which environment variables. A host can
use that to ask its user for a key; this module asks nobody.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from kicad_mcp.utils.component_search import (
    ComponentRecord,
    ComponentSearchClient,
    JLCSearchClient,
    _jlcsearch_limiter,
    _request_json,
    normalize_lcsc_code,
)
from kicad_mcp.utils.part_specs import (
    PartSpec,
    SpecFilter,
    canonical_name,
    merge_specs,
    specs_from_description,
    specs_from_jlcsearch,
)

#: The key-holding sources, the environment variables each reads, and what it adds.
KEYED_SOURCES: dict[str, tuple[tuple[str, ...], str]] = {
    "nexar": (
        ("NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"),
        "Octopart's catalog across distributors, with full specs, lifecycle and pricing",
    ),
    "digikey": (
        ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"),
        "DigiKey stock, pricing and parametric data",
    ),
    "mouser": (("MOUSER_API_KEY",), "Mouser stock, pricing and attributes"),
}

#: jlcsearch category inputs that filter on the server, per category: spec name to the
#: query parameter. Every other filter is applied to the returned rows.
_JLC_SERVER_FILTERS: dict[str, dict[str, str]] = {
    "capacitors": {"capacitance": "capacitance"},
    "resistors": {"resistance": "resistance"},
    "ldos": {"output_voltage": "output_voltage", "output_type": "output_type"},
    "leds": {"color": "color"},
    "microcontrollers": {"core": "core"},
}

#: Common words to jlcsearch category endpoints, so "capacitor" and "LDO" both work.
_JLC_CATEGORY_WORDS: dict[str, str] = {
    "capacitor": "capacitors",
    "cap": "capacitors",
    "mlcc": "capacitors",
    "resistor": "resistors",
    "ldo": "ldos",
    "regulator": "ldos",
    "led": "leds",
    "mcu": "microcontrollers",
    "microcontroller": "microcontrollers",
    "mosfet": "mosfets",
    "diode": "diodes",
    "buck": "buck_boost_converters",
    "boost": "boost_converters",
    "usb-c": "usb_c_connectors",
    "usb_c": "usb_c_connectors",
}

_JLC_PAGE_LIMIT = 100


@dataclass(frozen=True)
class SourceOutcome:
    """What one source did for a request."""

    source: str
    ok: bool
    count: int = 0
    reason: str = ""


@dataclass
class SourcingAnswer:
    """Records plus the account of every source asked."""

    records: list[ComponentRecord] = field(default_factory=list)
    outcomes: list[SourceOutcome] = field(default_factory=list)
    # A filter whose name no returned part has, with the spec names those parts do have.
    unmatched: dict[str, list[str]] = field(default_factory=dict)

    @property
    def answered_by(self) -> list[str]:
        return [item.source for item in self.outcomes if item.ok]

    @property
    def failed(self) -> list[SourceOutcome]:
        return [item for item in self.outcomes if not item.ok]

    def could_configure(self, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """Key-holding sources that are not configured, when every configured source failed."""
        if self.answered_by:
            return []
        return unconfigured_sources(env)

    def account(self, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """JSON-ready account of the sources: answered, failed (with reasons), could configure."""
        return {
            "answered_by": self.answered_by,
            "failed": [{"source": item.source, "reason": item.reason} for item in self.failed],
            "could_configure": self.could_configure(env),
        }


def configured_sources(env: Mapping[str, str] | None = None) -> list[str]:
    """``jlcsearch`` (no key) plus every key-holding source whose variables are all set."""
    values = os.environ if env is None else env
    found = ["jlcsearch"]
    for source, (variables, _adds) in KEYED_SOURCES.items():
        if all(str(values.get(name) or "").strip() for name in variables):
            found.append(source)
    return found


def unconfigured_sources(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Each key-holding source that is not configured: its variables and what it adds."""
    ready = set(configured_sources(env))
    return [
        {"source": source, "variables": list(variables), "adds": adds}
        for source, (variables, adds) in KEYED_SOURCES.items()
        if source not in ready
    ]


def resolve_sources(
    requested: Sequence[str] | None, env: Mapping[str, str] | None = None
) -> list[str]:
    """The sources to ask: the requested ones, or every configured one."""
    if not requested:
        return configured_sources(env)
    known = {"jlcsearch", *KEYED_SOURCES}
    out: list[str] = []
    for name in requested:
        folded = str(name).strip().casefold()
        if folded not in known:
            raise ValueError(f"Unknown source {name!r}. Use jlcsearch, nexar, digikey or mouser.")
        if folded not in out:
            out.append(folded)
    return out


def jlc_category(category: str) -> str:
    """The jlcsearch endpoint for a category name or common word."""
    folded = category.strip().casefold().replace(" ", "_")
    if not folded:
        raise ValueError("A category is needed, for example capacitors, resistors or ldos.")
    singular = folded[:-1] if folded.endswith("s") else folded
    return _JLC_CATEGORY_WORDS.get(folded) or _JLC_CATEGORY_WORDS.get(singular) or folded


def _server_value(spec_filter: SpecFilter) -> str | None:
    number, _unit = spec_filter.target()
    if number is not None:
        return f"{number:g}"
    return spec_filter.value


class JLCSearchCategories:
    """jlcsearch's typed category endpoints (``/<category>/list.json``)."""

    BASE = JLCSearchClient.BASE

    def __init__(
        self, request_json: Callable[[str, dict[str, object]], dict[str, Any]] | None = None
    ) -> None:
        self._request_json = request_json or _request_json

    def list(
        self,
        category: str,
        *,
        filters: Sequence[SpecFilter] = (),
        package: str = "",
        search: str = "",
    ) -> list[ComponentRecord]:
        """One page of the category, filtered on the server where jlcsearch can."""
        endpoint = jlc_category(category)
        server = _JLC_SERVER_FILTERS.get(endpoint, {})
        params: dict[str, object] = {
            "search": search or None,
            "package": package or None,
            "limit": str(_JLC_PAGE_LIMIT),
        }
        for item in filters:
            if item.op == "=" and item.name in server:
                params[server[item.name]] = _server_value(item)
        _jlcsearch_limiter.acquire()
        payload = self._request_json(f"{self.BASE}/{endpoint}/list.json", params)
        rows = payload.get(endpoint)
        if not isinstance(rows, list):
            raise ValueError(
                f"jlcsearch has no category {endpoint!r}; categories include capacitors, "
                "resistors, ldos, leds, mosfets, diodes and microcontrollers."
            )
        return [_record_from_category_row(row) for row in rows if isinstance(row, Mapping)]


def _record_from_category_row(row: Mapping[str, Any]) -> ComponentRecord:
    description = str(row.get("description", ""))
    price = row.get("price1", row.get("price"))
    specs = merge_specs(
        (specs_from_jlcsearch(row), specs_from_description(description, "jlcsearch"))
    )
    return ComponentRecord(
        source="jlcsearch",
        lcsc_code=normalize_lcsc_code(row.get("lcsc", "")),
        mpn=str(row.get("mfr", "")),
        package=str(row.get("package", "")),
        description=description,
        stock=int(row.get("stock", 0) or 0),
        price=float(price) if isinstance(price, (int, float)) else None,
        is_basic=bool(row.get("is_basic", False)),
        is_preferred=bool(row.get("is_preferred", False)),
        specs=specs,
    )


def _with_description_specs(record: ComponentRecord) -> ComponentRecord:
    if record.specs:
        return record
    return replace(record, specs=specs_from_description(record.description, record.source))


def _matches(
    record: ComponentRecord, filters: Sequence[SpecFilter], min_stock: int, package: str
) -> bool:
    if record.stock < min_stock:
        return False
    if package:
        folded = package.casefold()
        shown = [
            record.package,
            *(found.display for found in record.specs if found.name == "package"),
        ]
        if not any(folded in value.casefold() for value in shown if value):
            return False
    return all(item.matches(record.specs) for item in filters)


class ParametricService:
    """Parametric search, spec sheets and comparison over the configured sources."""

    def __init__(
        self,
        client_for: Callable[[str], ComponentSearchClient],
        *,
        categories: JLCSearchCategories | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._client_for = client_for
        self._categories = categories or JLCSearchCategories()
        self._env = env

    def sources(self, requested: Sequence[str] | None = None) -> list[str]:
        return resolve_sources(requested, self._env)

    def search(
        self,
        category: str,
        filters: Iterable[Mapping[str, Any]] = (),
        *,
        package: str = "",
        min_stock: int = 0,
        sources: Sequence[str] | None = None,
        limit: int = 20,
    ) -> SourcingAnswer:
        """Parts in ``category`` whose ``specs`` meet every filter, from each source."""
        parsed = [SpecFilter.from_mapping(item) for item in filters]
        answer = SourcingAnswer()
        for source in self.sources(sources):
            try:
                if source == "jlcsearch":
                    found = self._categories.list(category, filters=parsed, package=package)
                else:
                    words = " ".join(
                        [
                            category,
                            *(
                                item.value + item.unit
                                for item in parsed
                                if item.op in ("=", ">=", "contains")
                            ),
                        ]
                    )
                    found = [
                        _with_description_specs(item)
                        for item in self._client_for(source).search(
                            words, package=package or None, only_basic=False, limit=50
                        )
                    ]
            except (RuntimeError, ValueError, OSError) as exc:
                answer.outcomes.append(SourceOutcome(source, False, reason=_reason(exc)))
                continue
            seen = {spec.name for item in found for spec in item.specs}
            for item in parsed:
                if found and canonical_name(item.name) not in seen:
                    answer.unmatched[item.name] = sorted(seen)
            kept = [item for item in found if _matches(item, parsed, min_stock, package)]
            kept.sort(key=lambda item: (item.price is None, item.price or 0.0, -item.stock))
            answer.outcomes.append(SourceOutcome(source, True, count=len(kept)))
            answer.records.extend(kept)
        answer.records = answer.records[: max(1, limit)]
        return answer

    def spec_sheet(
        self, part: str, *, sources: Sequence[str] | None = None, category: str = ""
    ) -> tuple[list[ComponentRecord], tuple[PartSpec, ...], SourcingAnswer]:
        """One part from each source and every spec, merged with the source on each value."""
        answer = SourcingAnswer()
        records: list[ComponentRecord] = []
        for source in self.sources(sources):
            try:
                record = self._lookup(source, part, category)
            except (RuntimeError, ValueError, OSError) as exc:
                answer.outcomes.append(SourceOutcome(source, False, reason=_reason(exc)))
                continue
            if record is None:
                answer.outcomes.append(SourceOutcome(source, True, count=0, reason="not found"))
                continue
            answer.outcomes.append(SourceOutcome(source, True, count=1))
            records.append(record)
        answer.records = records
        return records, merge_specs(record.specs for record in records), answer

    def _lookup(self, source: str, part: str, category: str) -> ComponentRecord | None:
        record = self._client_for(source).get_part(part)
        if record is None:
            return None
        record = _with_description_specs(record)
        if source == "jlcsearch" and category:
            # The search API has no typed columns; find the same part in its category
            # page, narrowed by its package and the values its description gives.
            narrowing = [
                SpecFilter(found.name, "=", f"{found.number:g}", found.unit)
                for found in record.specs
                if found.number is not None
            ]
            try:
                rows = self._categories.list(category, filters=narrowing, package=record.package)
            except (RuntimeError, ValueError, OSError):
                return record
            for row in rows:
                if row.lcsc_code == record.lcsc_code or row.mpn.casefold() == record.mpn.casefold():
                    return replace(row, specs=merge_specs((row.specs, record.specs)))
        return record

    def compare(
        self,
        parts: Sequence[str],
        attributes: Sequence[str] = (),
        *,
        sources: Sequence[str] | None = None,
        category: str = "",
    ) -> tuple[list[str], list[dict[str, Any]], SourcingAnswer]:
        """Parts side by side: one row per attribute, one column per part."""
        from kicad_mcp.utils.part_specs import canonical_name  # noqa: PLC0415

        combined = SourcingAnswer()
        sheets: dict[str, tuple[PartSpec, ...]] = {}
        for part in parts:
            _records, specs, answer = self.spec_sheet(part, sources=sources, category=category)
            sheets[part] = specs
            combined.outcomes.extend(answer.outcomes)
        names = [canonical_name(name) for name in attributes] or sorted(
            {found.name for specs in sheets.values() for found in specs}
        )
        rows: list[dict[str, Any]] = []
        for name in names:
            row: dict[str, Any] = {"attribute": name}
            for part, specs in sheets.items():
                values = [found for found in specs if found.name == name]
                row[part] = [{"display": found.display, "source": found.source} for found in values]
            rows.append(row)
        return list(parts), rows, combined


def _reason(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    status = getattr(exc, "code", None)
    if isinstance(status, int) and status == 429:
        return f"rate limited (HTTP 429): {text}"
    return text


#: Derating parameters to the spec names that hold the rating, in order of preference.
_RATING_SPECS: dict[str, tuple[str, ...]] = {
    "voltage": ("voltage_rating", "input_voltage_max", "max_overload_voltage"),
    "current": ("current_rating", "output_current", "ripple_current"),
    "power": ("power_rating",),
    "temperature": ("operating_temp_max", "operating_temperature"),
}


def rating_from_specs(
    part: str, parameter: str, service: ParametricService | None = None
) -> PartSpec | str:
    """The rated value for ``parameter`` from ``part``'s specs, or why there is none."""
    from kicad_mcp.utils.part_specs import first_number  # noqa: PLC0415

    if not part.strip():
        return "give rated_value, or part to read the rating from its specs"
    names = _RATING_SPECS.get(parameter.strip().casefold(), (parameter,))
    if service is None:
        from kicad_mcp.tools.library import _component_search_client  # noqa: PLC0415

        service = ParametricService(_component_search_client)
    _records, specs, answer = service.spec_sheet(part)
    for name in names:
        if (found := first_number(specs, name)) is not None:
            return found
    sources = ", ".join(answer.answered_by) or "no source"
    return (
        f"no {parameter} rating in the specs of {part} from {sources}; give rated_value "
        "(for example from the part's datasheet)"
    )


def sourcing_hint(exc: BaseException | None = None, env: Mapping[str, str] | None = None) -> str:
    """A line for a failed sourcing result: the key-holding sources that could be set up.

    Empty for a failure that is not the source's (a missing file is the project's).
    """
    if isinstance(exc, FileNotFoundError):
        return ""
    rows = unconfigured_sources(env)
    if not rows:
        return ""
    listed = "; ".join(
        f"{row['source']} ({', '.join(row['variables'])}) adds {row['adds']}" for row in rows
    )
    return f"\nOther part sources can be configured: {listed}."
