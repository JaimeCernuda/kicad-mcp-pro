"""FastMCP adapter for parametric part search, spec sheets and part comparison."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer as FastMCP

from ..errors import ToolFailedError
from ..library.parametric import ParametricService, SourcingAnswer
from ..utils.component_search import ComponentRecord, stock_text
from ..utils.part_specs import PartSpec
from .metadata import headless_compatible


@dataclass(frozen=True)
class LibraryParametricDependencies:
    service: ParametricService


def _key_specs(record: ComponentRecord, limit: int = 6) -> str:
    shown: list[str] = []
    seen: set[str] = set()
    for found in record.specs:
        if (
            found.name in seen
            or found.name == "package"
            or found.number is None
            and not found.display
        ):
            continue
        seen.add(found.name)
        shown.append(f"{found.name}={found.display}")
        if len(shown) >= limit:
            break
    return ", ".join(shown)


def _account_lines(answer: SourcingAnswer) -> list[str]:
    account = answer.account()
    lines = [f"Answered by: {', '.join(account['answered_by']) or 'no source'}."]
    for failed in account["failed"]:
        lines.append(f"{failed['source']} failed: {failed['reason']}")
    for row in account["could_configure"]:
        lines.append(
            f"{row['source']} is not configured; it would add {row['adds']} "
            f"(set {', '.join(row['variables'])})."
        )
    return lines


def format_search(category: str, answer: SourcingAnswer) -> str:
    lines = [f"Parametric search for {category}: {len(answer.records)} parts meet every filter."]
    for record in answer.records:
        code = record.lcsc_code or "-"
        price = f"{record.price:g}" if record.price is not None else "?"
        lines.append(
            f"- {record.mpn} ({code}, {record.source}) package={record.package or '?'} "
            f"stock={stock_text(record)} price={price}; {_key_specs(record)}"
        )
    for name, names in answer.unmatched.items():
        lines.append(
            f"No part returned has a spec named {name!r}, so that filter matched nothing. "
            f"Spec names these parts have: {', '.join(names) or 'none'}."
        )
    lines.extend(_account_lines(answer))
    lines.append("Specs: " + json.dumps(_records_json(answer.records), ensure_ascii=False))
    return "\n".join(lines)


def _records_json(records: list[ComponentRecord]) -> list[dict[str, Any]]:
    return [
        {
            "mpn": record.mpn,
            "lcsc": record.lcsc_code,
            "source": record.source,
            "package": record.package,
            "stock": record.stock,
            "price": record.price,
            "specs": [found.as_dict() for found in record.specs],
        }
        for record in records
    ]


def format_spec_sheet(part: str, specs: tuple[PartSpec, ...], answer: SourcingAnswer) -> str:
    if not specs:
        lines = [f"No specs found for {part}."]
    else:
        lines = [f"Spec sheet for {part} ({len(specs)} values):"]
        lines.extend(f"- {found.name}: {found.display} [{found.source}]" for found in specs)
    lines.extend(_account_lines(answer))
    return "\n".join(lines)


def format_compare(parts: list[str], rows: list[dict[str, Any]], answer: SourcingAnswer) -> str:
    lines = ["| attribute | " + " | ".join(parts) + " |", "|---" * (len(parts) + 1) + "|"]
    for row in rows:
        cells = []
        for part in parts:
            values = row.get(part) or []
            cells.append(
                "; ".join(f"{value['display']} [{value['source']}]" for value in values) or "-"
            )
        lines.append(f"| {row['attribute']} | " + " | ".join(cells) + " |")
    lines.extend(_account_lines(answer))
    return "\n".join(lines)


def register(mcp: FastMCP, deps: LibraryParametricDependencies) -> None:
    """Register parametric search, spec sheet and comparison tools."""

    @mcp.tool()
    @headless_compatible
    def lib_search_parametric(
        category: str,
        filters: list[dict[str, Any]] | None = None,
        package: str = "",
        min_stock: int = 0,
        sources: list[str] | None = None,
        limit: int = 20,
    ) -> str:
        """Find parts in a category whose specs meet every filter, across part sources.

        ``filters`` are ``{name, op, value, unit}`` with ``op`` one of ``=``, ``!=``,
        ``>=``, ``<=``, ``>``, ``<`` or ``contains``; values compare in SI units, so
        ``10 uF`` matches ``10µF`` and ``0.01 mF``. ``category`` is a jlcsearch category
        (capacitors, resistors, ldos, leds, mosfets, diodes, microcontrollers, ...).
        ``sources`` defaults to every configured source (jlcsearch needs no key; nexar,
        digikey and mouser need theirs). The result names the sources that answered,
        why any failed, and which unconfigured sources could be set up.
        """
        try:
            answer = deps.service.search(
                category,
                filters or [],
                package=package,
                min_stock=min_stock,
                sources=sources,
                limit=limit,
            )
        except ValueError as exc:
            raise ToolFailedError(f"Parametric search failed: {exc}") from exc
        return format_search(category, answer)

    @mcp.tool()
    @headless_compatible
    def lib_get_component_specs(
        part: str,
        sources: list[str] | None = None,
        category: str = "",
    ) -> str:
        """Get one part's full spec sheet, merged across the configured part sources.

        ``part`` is a manufacturer part number or an LCSC code. Every value keeps the
        source that reported it, so agreement and disagreement between sources stay
        visible; values read from a description are marked ``<source>:description``.
        Give ``category`` (e.g. capacitors) to also read jlcsearch's typed columns.
        """
        try:
            _records, specs, answer = deps.service.spec_sheet(
                part, sources=sources, category=category
            )
        except ValueError as exc:
            raise ToolFailedError(f"Spec lookup failed: {exc}") from exc
        return format_spec_sheet(part, specs, answer)

    @mcp.tool()
    @headless_compatible
    def lib_compare_parts(
        parts: list[str],
        attributes: list[str] | None = None,
        sources: list[str] | None = None,
        category: str = "",
    ) -> str:
        """Compare parts side by side on the attributes given (every attribute by default).

        Returns a table with one row per attribute and one column per part; each cell
        lists the values with their sources.
        """
        if not parts:
            raise ToolFailedError("Part comparison failed: give at least one part.")
        try:
            names, rows, answer = deps.service.compare(
                parts, attributes or [], sources=sources, category=category
            )
        except ValueError as exc:
            raise ToolFailedError(f"Part comparison failed: {exc}") from exc
        return format_compare(names, rows, answer)
