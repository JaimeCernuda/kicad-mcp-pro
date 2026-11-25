"""KiCad CLI footprint tools.

Wraps ``kicad-cli fp`` subcommands for headless footprint export and format
upgrade, and reads footprint files directly for inspection.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from mcp.server.mcpserver import MCPServer as FastMCP

from ..config import get_config
from ..models.contract_verifier import parse_footprint
from .export_support import _ensure_output_dir, _run_cli, _run_cli_variants
from .metadata import headless_compatible


@headless_compatible
def fp_export(
    output_path: str = "",
    format: str = "step",
) -> str:
    """Export a KiCad footprint to an interchange format.

    Supported formats depend on the installed ``kicad-cli`` version.
    Common values: ``step``, ``svg``, ``dxf``, ``wrl`` (VRML).

    Parameters
    ----------
    output_path : str
        Optional output file name (relative to the export directory).
        Defaults to ``<footprint_name>.<format>``.
    format : str
        Target export format.  Defaults to ``step``.
    """
    cfg = get_config()
    if cfg.pcb_file is None:
        return "No PCB file configured. Call kicad_set_project() first."

    out_dir = _ensure_output_dir("footprints")
    board_stem = cfg.pcb_file.stem
    out_name = Path(output_path.strip() or f"{board_stem}.{format}").name
    out_file = out_dir / out_name

    code, _, stderr = _run_cli_variants(
        [
            [
                "fp",
                "export",
                format,
                "--output",
                str(out_file),
                str(cfg.pcb_file),
            ],
            [
                "fp",
                "export",
                format,
                "--input",
                str(cfg.pcb_file),
                "--output",
                str(out_file),
            ],
        ]
    )
    if code != 0:
        return f"Footprint export failed: {stderr or 'unknown error'}"
    return f"Footprint exported to {out_file}"


def _build_fp_export_svg_cmd(
    out_dir: Path,
    in_path: Path,
    footprint: str,
    layers: list[str] | None,
    theme: str,
    black_and_white: bool,
) -> list[str]:
    cmd = ["fp", "export", "svg", "--output", str(out_dir)]
    if footprint:
        cmd.extend(["--footprint", footprint])
    if layers:
        cmd.extend(["--layers", ",".join(layers)])
    if theme:
        cmd.extend(["--theme", theme])
    if black_and_white:
        cmd.append("--black-and-white")
    cmd.append(str(in_path))
    return cmd


def _layer_list(layers: list[str] | str | None) -> list[str]:
    items = layers.split(",") if isinstance(layers, str) else layers or []
    return [item.strip() for item in items if item.strip()]


@headless_compatible
def fp_export_svg(
    input_path: str,
    output_dir: str = "",
    footprint: str = "",
    layers: list[str] | str | None = None,
    theme: str = "",
    black_and_white: bool = False,
) -> str:
    """Export a footprint or footprint library to SVG format.

    Parameters
    ----------
    input_path : str
        Path to the footprint file (.kicad_mod) or library directory (.pretty).
    output_dir : str
        Optional output directory.
    footprint : str
        Specific footprint name to export within the library.
    layers : list[str] | str
        Optional layers to include: one name such as ``F.Cu``, a list, or a
        comma-separated string.
    theme : str
        Optional color theme name.
    black_and_white : bool
        Export in black and white only.
    """
    cfg = get_config()
    if not input_path:
        return "Error: input_path parameter is required."

    try:
        from ..path_safety import resolve_under

        if cfg.project_dir is not None:
            in_path = resolve_under(cfg.project_dir, input_path)
        else:
            in_path = Path(input_path).expanduser().resolve()
    except Exception as exc:
        raise ValueError(f"Unsafe input path: {exc}") from exc

    if not in_path.exists():
        return f"Input footprint file or directory not found: {input_path}"

    out_dir = _ensure_output_dir("footprints")
    if output_dir:
        try:
            if cfg.project_dir is not None:
                out_dir = resolve_under(cfg.project_dir, output_dir)
            else:
                out_dir = Path(output_dir).expanduser().resolve()
        except Exception as exc:
            raise ValueError(f"Unsafe output directory: {exc}") from exc

    cmd = _build_fp_export_svg_cmd(
        out_dir, in_path, footprint, _layer_list(layers), theme, black_and_white
    )

    code, stdout, stderr = _run_cli(*cmd)
    if code != 0:
        return f"Footprint SVG export failed: {stderr or stdout or 'unknown error'}"
    return f"Footprint SVG exported successfully to {out_dir}"


def _resolve_fp_upgrade_output_path(
    output_file: str, in_path_obj: Path, project_dir: Path | None
) -> str:
    out_path = output_file.strip() if output_file else ""
    if out_path:
        try:
            if project_dir is not None:
                from ..path_safety import resolve_under

                return str(resolve_under(project_dir, out_path))
            return str(Path(out_path).expanduser().resolve())
        except Exception as exc:
            raise ValueError(f"Unsafe output path: {exc}") from exc
    out_dir = _ensure_output_dir("upgraded")
    return str(out_dir / in_path_obj.name)


@headless_compatible
def fp_upgrade(
    input_file: str = "",
    output_file: str = "",
    input_path: str = "",
    force: bool = False,
    dry_run: bool = True,
    in_place: bool = False,
) -> str:
    """Upgrade a KiCad footprint library file to the current file format.

    Parameters
    ----------
    input_file : str
        Legacy input file parameter (for backward compatibility).
    output_file : str
        Legacy output file parameter (for backward compatibility).
    input_path : str
        Path to the footprint library to upgrade. If omitted, input_file is used.
    force : bool
        Force resaving regardless of version.
    dry_run : bool
        Only report what would be written; pass ``False`` to write it.
    in_place : bool
        Rewrite the library itself instead of writing an upgraded copy.
    """
    cfg = get_config()
    raw_in = input_path or input_file
    if not raw_in and cfg.pcb_file is None:
        return (
            "No input file provided and no PCB configured. "
            "Provide an input_path or call kicad_set_project() first."
        )

    in_file = raw_in or str(cfg.pcb_file)

    try:
        from ..path_safety import resolve_under

        if cfg.project_dir is not None:
            in_path_obj = resolve_under(cfg.project_dir, in_file)
        else:
            in_path_obj = Path(in_file).expanduser().resolve()
    except Exception as exc:
        raise ValueError(f"Unsafe input path: {exc}") from exc

    out_path = (
        str(in_path_obj)
        if in_place
        else _resolve_fp_upgrade_output_path(output_file, in_path_obj, cfg.project_dir)
    )

    if dry_run:
        return (
            f"Dry run: Would upgrade footprint library '{in_path_obj}' "
            f"and save to '{out_path}' (force={force}). Nothing was written; "
            "call again with dry_run=False to write it"
            + ("." if in_place else ", adding in_place=True to rewrite the library itself.")
        )

    cmd = ["fp", "upgrade"]
    if force:
        cmd.append("--force")
    if not in_place:
        cmd.extend(["--output", out_path])
    cmd.append(str(in_path_obj))

    code, stdout, stderr = _run_cli(*cmd)
    if code != 0:
        return f"Footprint upgrade failed: {stderr or stdout or 'unknown error'}"
    return f"Footprint upgraded and saved to {out_path}"


def _footprint_facts(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    shape = parse_footprint(text)
    pad_kinds = Counter(re.findall(r'\(pad\s+(?:"[^"]*"|[^\s()]+)\s+(\w+)', text))
    name = re.match(r'\s*\(footprint\s+"([^"]+)"', text)
    version = re.search(r"\(version\s+(\d+)\)", text)
    layer = re.search(r'\(layer\s+"([^"]+)"\)', text)
    descr = re.search(r'\(descr\s+"((?:[^"\\]|\\.)*)"', text)
    tags = re.search(r'\(tags\s+"((?:[^"\\]|\\.)*)"', text)
    attr = re.search(r"\(attr\s+([^()]*)\)", text)
    models = re.findall(r'\(model\s+"([^"]+)"', text)
    kinds = ", ".join(f"{count} {kind}" for kind, count in sorted(pad_kinds.items()))
    pads = ", ".join(shape.connectable_pads) or "(none)"
    lines = [
        f"Footprint: {name.group(1) if name else path.stem}",
        f"- File: {path}",
        f"- Format version: {version.group(1) if version else '(none)'}",
        f"- Layer: {layer.group(1) if layer else '(none)'}",
        f"- Description: {descr.group(1) if descr else '(none)'}",
        f"- Tags: {tags.group(1) if tags else '(none)'}",
        f"- Attributes: {attr.group(1).strip() if attr else '(none)'}",
        f"- Pads: {sum(pad_kinds.values())} ({kinds or 'none'}); numbered: {pads}",
        f"- Courtyard: {'yes' if shape.has_courtyard else 'no'}, "
        f"fabrication: {'yes' if shape.has_fabrication else 'no'}, "
        f"silkscreen: {'yes' if shape.has_silkscreen else 'no'}",
        f"- 3D models: {', '.join(models) if models else '(none)'}",
    ]
    if "(fp_text reference" in text or "(fp_text value" in text:
        lines.append(
            "- Older file format: run fp_upgrade on it (dry_run=False, in_place=True) "
            "before placing it with current KiCad."
        )
    return "\n".join(lines)


def _saved_board_footprint_summary(pcb_file: Path) -> str:
    text = pcb_file.read_text(encoding="utf-8", errors="ignore")
    used = Counter(re.findall(r'\n\s*\(footprint\s+"([^"]+)"', text))
    lines = [f"Footprints on {pcb_file.name} ({sum(used.values())} placed, {len(used)} kinds):"]
    lines.extend(f"- {lib_id} x{count}" for lib_id, count in sorted(used.items()))
    lines.append("Pass input_path (a .kicad_mod, or a .pretty with footprint) for one footprint.")
    return "\n".join(lines)


@headless_compatible
def fp_get_info(input_path: str = "", footprint: str = "") -> str:
    """Describe a footprint file, or list the footprints on the active board.

    KiCad's CLI has no footprint query, so this reads the files directly.

    Parameters
    ----------
    input_path : str
        A ``.kicad_mod`` file, or a ``.pretty`` library together with ``footprint``.
        Leave empty to list the footprints placed on the active board.
    footprint : str
        Footprint name inside the ``.pretty`` library given as ``input_path``.
    """
    cfg = get_config()
    if not input_path:
        if cfg.pcb_file is None:
            return "No PCB file configured. Call kicad_set_project() first, or pass input_path."
        if not cfg.pcb_file.exists():
            return f"PCB file not found: {cfg.pcb_file}"
        return _saved_board_footprint_summary(cfg.pcb_file)

    try:
        from ..path_safety import resolve_under

        if cfg.project_dir is not None:
            in_path = resolve_under(cfg.project_dir, input_path)
        else:
            in_path = Path(input_path).expanduser().resolve()
    except Exception as exc:
        raise ValueError(f"Unsafe input path: {exc}") from exc

    if in_path.is_dir():
        if not footprint:
            names = sorted(path.stem for path in in_path.glob("*.kicad_mod"))
            listed = "\n".join(f"- {name}" for name in names)
            return f"Footprints in {in_path.name} ({len(names)}); pass footprint:\n{listed}"
        in_path = in_path / f"{Path(footprint).stem}.kicad_mod"
    if not in_path.is_file():
        return f"Footprint file not found: {in_path}"
    return _footprint_facts(in_path)


def register(mcp: FastMCP) -> None:
    """Register footprint management tools."""
    mcp.tool()(fp_export)
    mcp.tool()(fp_export_svg)
    mcp.tool()(fp_upgrade)
    mcp.tool()(fp_get_info)
