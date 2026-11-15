"""What the views read: board text, schematic files, GLB, BOM, checks, stackup, parts.

Each builder returns plain JSON-ready data. Board text comes from the live KiCad when
one serves the board, else from the board file, and the result says which (KI6).
"""

from __future__ import annotations

import base64
import json
import re
import subprocess  # nosec B404 - argv-only kicad-cli export, shell=False
import tempfile
from pathlib import Path
from typing import Any

from ..config import get_config

#: Larger GLB files are not sent to a view.
MAX_GLB_BYTES = 40 * 1024 * 1024


def project_root() -> Path:
    cfg = get_config()
    if cfg.project_dir is not None:
        return Path(cfg.project_dir)
    if cfg.pcb_file is not None:
        return Path(cfg.pcb_file).parent
    raise ValueError("No project is set. Call kicad_set_project first.")


def schematic_paths() -> list[Path]:
    cfg = get_config()
    root = project_root()
    found: list[Path] = []
    if cfg.sch_file is not None and Path(cfg.sch_file).is_file():
        found.append(Path(cfg.sch_file))
    for path in sorted(root.rglob("*.kicad_sch")):
        if path not in found and not path.name.startswith(("~", "_autosave")):
            found.append(path)
    return found


def live_board_text() -> str | None:
    """The live board's text when a KiCad serves it, else ``None``."""
    from ..connection import get_board  # noqa: PLC0415

    try:
        return str(get_board().get_as_string())
    except Exception:  # noqa: BLE001 - any IPC failure means "no live board"
        return None


def board_data() -> dict[str, Any]:
    cfg = get_config()
    if cfg.pcb_file is None:
        raise ValueError("The project has no board file (.kicad_pcb).")
    board = Path(cfg.pcb_file)
    text = live_board_text()
    if text is not None:
        return {"board_name": board.name, "text": text, "source": "live"}
    from ..instance import get_instance_manager  # noqa: PLC0415

    status = get_instance_manager().status()
    reason = status.problem or "no KiCad instance serves this board"
    return {
        "board_name": board.name,
        "text": board.read_text(encoding="utf-8"),
        "source": "file",
        "note": f"Edits go to the board file: {reason}.",
    }


def schematic_data() -> dict[str, Any]:
    root = project_root()
    paths = schematic_paths()
    if not paths:
        raise ValueError("The project has no schematic (.kicad_sch).")
    files = [
        {
            "name": str(path.relative_to(root)) if path.is_relative_to(root) else path.name,
            "text": path.read_text(encoding="utf-8"),
        }
        for path in paths
    ]
    return {"root": files[0]["name"], "files": files}


def glb_data() -> dict[str, Any]:
    cfg = get_config()
    if cfg.pcb_file is None:
        raise ValueError("The project has no board file (.kicad_pcb).")
    with tempfile.TemporaryDirectory(prefix="kicad-views-") as tmp:
        out = Path(tmp) / "board.glb"
        source = Path(cfg.pcb_file)
        text = live_board_text()
        if text is not None:
            # Export what the live KiCad shows, not a stale file.
            source = Path(tmp) / Path(cfg.pcb_file).name
            source.write_text(text, encoding="utf-8")
        from ..instance import get_instance_manager  # noqa: PLC0415

        # The board text comes from the KiCad in use: export with its own CLI, which can
        # read its file format (a KiCad 11 board is newer than KiCad 10's CLI reads).
        cli = get_instance_manager().status().cli or str(cfg.kicad_cli)
        argv = [
            cli,
            "pcb",
            "export",
            "glb",
            "--output",
            str(out),
            "--include-tracks",
            "--include-pads",
            "--include-zones",
            "--subst-models",
            str(source),
        ]
        result = subprocess.run(  # noqa: S603  # nosec B603
            argv, capture_output=True, text=True, timeout=180, check=False
        )
        if result.returncode != 0 or not out.is_file():
            detail = (result.stderr or result.stdout).strip()[:400]
            raise ValueError(f"kicad-cli could not export the 3D model: {detail}")
        data = out.read_bytes()
    if len(data) > MAX_GLB_BYTES:
        raise ValueError(f"The 3D model is {len(data) // 1024 // 1024} MB, over the view's limit.")
    return {"glb_base64": base64.b64encode(data).decode("ascii"), "size": len(data)}


def bom_data(quantity: int = 1, sourcing: bool = True) -> dict[str, Any]:
    from ..library.parametric import ParametricService, SourcingAnswer  # noqa: PLC0415
    from ..tools.library import (  # noqa: PLC0415
        _component_search_client,
        _group_bom_rows,
        _schematic_component_rows,
    )

    grouped = _group_bom_rows(_schematic_component_rows())
    service = ParametricService(_component_search_client)
    account = SourcingAnswer()
    rows: list[dict[str, Any]] = []
    for row in grouped:
        code = str(row.get("lcsc") or row.get("mpn") or "")
        record = None
        if sourcing and code:
            records, specs, answer = service.spec_sheet(
                code, sources=["jlcsearch"] if row.get("lcsc") else None
            )
            account.outcomes.extend(answer.outcomes)
            record = records[0] if records else None
        else:
            specs = ()
        populate = str(row.get("populate") or "").casefold()
        rows.append(
            {
                "references": list(row.get("references") or []),
                "value": row.get("value", ""),
                "footprint": row.get("footprint", ""),
                "mpn": row.get("mpn", "") or (record.mpn if record else ""),
                "lcsc": row.get("lcsc", ""),
                "manufacturer": row.get("manufacturer", ""),
                "dnp": populate in ("dnp", "no", "false", "0"),
                "stock": record.stock if record and record.stock_reported else None,
                "price": (record.price * 1.0) if record and record.price is not None else None,
                "currency": record.currency if record else "",
                "lifecycle": record.lifecycle if record else "",
                "datasheet": record.datasheet_url if record else "",
                "specs": [
                    {"name": spec.name, "display": spec.display, "source": spec.source}
                    for spec in specs
                ],
            }
        )
    seen: set[tuple[str, bool]] = set()
    unique = []
    for outcome in account.outcomes:
        if (outcome.source, outcome.ok) not in seen:
            seen.add((outcome.source, outcome.ok))
            unique.append(outcome)
    account.outcomes = unique
    return {"quantity": quantity, "rows": rows, "sources": account.account()}


def _violations(report: dict[str, Any] | None, kind: str) -> list[dict[str, Any]]:
    if not report:
        return []
    raw: list[dict[str, Any]] = list(report.get("violations") or [])
    for sheet in report.get("sheets") or []:
        raw.extend(sheet.get("violations") or [])
    out: list[dict[str, Any]] = []
    for item in raw:
        items = []
        for part in item.get("items") or []:
            pos = part.get("pos") or {}
            entry: dict[str, Any] = {
                "description": part.get("description", ""),
                "uuid": part.get("uuid"),
            }
            if "x" in pos and "y" in pos:
                entry["position_mm"] = {
                    "x": round(float(pos["x"]), 3),
                    "y": round(float(pos["y"]), 3),
                }
            items.append(entry)
        out.append(
            {
                "kind": kind,
                "rule": item.get("type", "rule"),
                "severity": item.get("severity", "error"),
                "description": item.get("description", ""),
                "items": items,
            }
        )
    return out


def checks_data(kind: str = "both", run: bool = True) -> dict[str, Any]:
    from ..tools.validation import _run_drc_report, _run_erc_report  # noqa: PLC0415

    kinds = ("drc", "erc") if kind == "both" else (kind,)
    violations: list[dict[str, Any]] = []
    summaries: dict[str, str] = {}
    unconnected = 0
    for name in kinds:
        runner = _run_drc_report if name == "drc" else _run_erc_report
        path, report, error = runner(f"{name}_report.json")
        if error and not report:
            summaries[name] = f"could not run: {error}"
            continue
        found = _violations(report, name)
        violations.extend(found)
        loose = report.get("unconnected_items") if name == "drc" and report else None
        if isinstance(loose, list):
            unconnected = len(loose)
            for item in loose:
                violations.extend(_violations({"violations": [item]}, "drc"))
        summaries[name] = f"{len(found)} violations" + (
            f", {unconnected} unconnected" if name == "drc" else ""
        )
        _ = path
    _ = run
    return {
        "ran": list(kinds),
        "violations": violations,
        "unconnected": unconnected,
        "summaries": summaries,
    }


def stackup_data() -> dict[str, Any]:
    from ..tools.pcb import _current_stackup_specs  # noqa: PLC0415

    layers = [
        {
            "name": spec.name,
            "type": spec.type,
            "material": spec.material,
            "thickness_mm": spec.thickness_mm,
            "epsilon_r": spec.epsilon_r,
            "loss_tangent": spec.loss_tangent,
        }
        for spec in _current_stackup_specs()
    ]
    total = sum(float(layer["thickness_mm"] or 0) for layer in layers) or None
    return {
        "layers": layers,
        "board_thickness_mm": round(total, 4) if total else None,
        "net_classes": _net_classes(),
    }


def _net_classes() -> list[dict[str, Any]]:
    cfg = get_config()
    project_file = getattr(cfg, "project_file", None)
    if not project_file or not Path(project_file).is_file():
        return []
    try:
        data = json.loads(Path(project_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    classes = ((data.get("net_settings") or {}).get("classes")) or []
    return [
        {
            "name": row.get("name", ""),
            "track_width_mm": row.get("track_width"),
            "clearance_mm": row.get("clearance"),
            "via_diameter_mm": row.get("via_diameter"),
            "via_drill_mm": row.get("via_drill"),
        }
        for row in classes
        if isinstance(row, dict)
    ]


def parts_data(
    category: str,
    filters: list[dict[str, Any]],
    package: str = "",
    min_stock: int = 0,
    candidates: list[str] | None = None,
) -> dict[str, Any]:
    from ..library.parametric import ParametricService, SourcingAnswer  # noqa: PLC0415
    from ..tools.library import _component_search_client  # noqa: PLC0415

    service = ParametricService(_component_search_client)
    if candidates:
        answer = SourcingAnswer()
        records = []
        for part in candidates:
            found, _specs, one = service.spec_sheet(part, category=category)
            answer.outcomes.extend(one.outcomes)
            records.extend(found[:1])
        answer.records = records
    else:
        answer = service.search(category, filters, package=package, min_stock=min_stock, limit=60)
    rows = [
        {
            "mpn": record.mpn,
            "lcsc": record.lcsc_code,
            "source": record.source,
            "package": record.package,
            "stock": record.stock if record.stock_reported else None,
            "price": record.price,
            "specs": [
                {"name": s.name, "display": s.display, "source": s.source} for s in record.specs
            ],
        }
        for record in answer.records
    ]
    return {"rows": rows, "account": answer.account()}


def compare_data(parts: list[str], category: str = "") -> dict[str, Any]:
    from ..library.parametric import ParametricService  # noqa: PLC0415
    from ..tools.library import _component_search_client  # noqa: PLC0415

    names, rows, answer = ParametricService(_component_search_client).compare(
        parts, category=category
    )
    return {"parts": names, "rows": rows, "account": answer.account()}


_SUMMARY_COUNTS = (
    ("footprints", re.compile(r"^\s*\(footprint\s", re.M)),
    ("tracks", re.compile(r"^\s*\(segment\s", re.M)),
)
# A net as a file declares it, ``(net 3 "GND")``, or as KiCad 10 writes a live board,
# ``(net "GND")``; the unnamed net is not counted.
_NET_NAME = re.compile(r'\(net\s+(?:\d+\s+)?"([^"]+)"\s*\)')


def board_summary(text: str) -> dict[str, int]:
    """Counts a view's opening result reports to the model."""
    counts = {name: len(pattern.findall(text)) for name, pattern in _SUMMARY_COUNTS}
    counts["nets"] = len(set(_NET_NAME.findall(text)))
    return counts
