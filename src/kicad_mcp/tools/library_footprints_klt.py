"""Footprints and 3D models from datasheets with KiCad Library Tools (footprints plan FP1-FP6).

The tools hand a generator family an entry written from the datasheet's package drawing and
land pattern, file what it makes into the project library, and check a footprint against the
KiCad Library Conventions and against the datasheet's pads. Library Tools runs as a child
process (``library_tools.install``); this module never imports it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from ..config import get_config
from ..library_resolution import footprint_file
from ..library_tools import install, runner
from .metadata import headless_compatible

_MAX_LISTED = 120


def _project() -> tuple[Path, str]:
    cfg = get_config()
    if cfg.project_dir is None:
        raise runner.LibraryToolsError("No project is set; call kicad_set_project first.")
    project_dir = Path(cfg.project_dir)
    name = cfg.project_file.stem if cfg.project_file else project_dir.name
    return project_dir, name


def _tools(*, models: bool) -> install.Installed:
    """Library Tools, with 3D models when they install; a footprint never waits on a body."""
    try:
        return install.ensure(models=models)
    except install.LibraryToolsInstallError as exc:
        if not models:
            raise
        tools = install.ensure(models=False)
        note = f"No 3D bodies this time: {exc.reason} The footprints are made without them."
        return replace(tools, notes=(*tools.notes, note))


def _notes(tools: install.Installed) -> list[str]:
    return [f"Setup: {note}" for note in tools.notes]


def _pads(rows: list[dict[str, Any]]) -> list[runner.Pad]:
    pads: list[runner.Pad] = []
    for row in rows:
        try:
            pads.append(
                runner.Pad(
                    number=str(row["number"]),
                    x=float(row["x"]),
                    y=float(row["y"]),
                    w=float(row["w"]),
                    h=float(row["h"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise runner.LibraryToolsError(
                f"Each land-pattern pad needs number, x, y, w and h in mm; got {row!r}."
            ) from exc
    return pads


def _filed(
    generated: runner.Generated,
    *,
    library: str,
    rung: str,
    datasheet: str,
    source: str,
    tools: install.Installed,
) -> str:
    project_dir, project = _project()
    library = library or project
    footprints, models, notes = runner.file_into_project(generated, project_dir, library)
    for path in footprints:
        runner.update_record(
            project_dir,
            path.stem,
            library=library,
            rung=rung,
            source=source,
            datasheet=datasheet,
            model=next((m.name for m in models if m.stem == path.stem), ""),
            library_tools=tools.pin,
            accepted=rung != "custom",
            check=None,
        )
    lines = [
        *_notes(tools),
        f"Wrote {len(footprints)} footprint(s) to libs/{library}.pretty and {len(models)} "
        "3D body(ies) to libs/3d (model paths are relative to the project; the library is in "
        "the project's fp-lib-table):",
        *(f"- {library}:{path.stem}" for path in footprints),
        *(f"- body libs/3d/{path.name}" for path in models),
        *notes,
        "Next: lib_check_footprint each one against the datasheet's land pattern before "
        "assigning it.",
    ]
    if rung == "custom":
        lines.append(
            "This is a custom footprint: show it to the person beside the datasheet's land "
            "pattern; it is assigned only after they accept it."
        )
    if generated.messages:
        lines += ["Generator output:", generated.messages]
    return "\n".join(lines)


def _footprint_path(path: str) -> Path:
    if ":" in path and not path.endswith(".kicad_mod") and not Path(path).is_absolute():
        library, name = path.split(":", 1)
        return footprint_file(library, name)
    return get_config().resolve_within_project(path)


def _failure(exc: Exception) -> str:
    return f"Not done: {exc}"


def register(mcp: FastMCP) -> None:
    """Register the Library Tools footprint tools."""

    @mcp.tool()
    @headless_compatible
    def lib_list_footprint_families(query: str = "", family: str = "") -> str:
        """List KiCad Library Tools' footprint generator families, or show one family's format.

        Without ``family``: the families (filtered by ``query``, e.g. "no_lead", "gullwing",
        "connector"), each marked with whether it makes footprints and 3D models and how
        many data files it has. With ``family``: where its entry keys are read (the schema is
        the code) and the head of a real data file, to copy the entry format from. The first
        call may install Library Tools into the tools folder.
        """
        try:
            tools = _tools(models=False)
        except install.LibraryToolsInstallError as exc:
            return _failure(exc)
        if family:
            example = runner.family_example(tools, family)
            if not example:
                return (
                    f"The {family} family has no YAML data at this pin: it cannot take an "
                    "entry. Use lib_generate_footprint_custom for such a part."
                )
            readers = ", ".join(runner.family_readers(tools, family))
            return "\n".join(
                [
                    *_notes(tools),
                    f"Family {family} (KiCad Library Tools {tools.pin[:10]}).",
                    f"Its entry keys are read in: {readers}.",
                    "An entry is one YAML mapping keyed by the footprint's name; take every "
                    "dimension from the datasheet's package drawing and recommended land "
                    "pattern, with the page in `size_source` or `datasheet`.",
                    "Example data file:",
                    example,
                ]
            )
        rows = [f for f in runner.families(tools) if query.lower() in f.name.lower()]
        lines = [*_notes(tools), f"{len(rows)} generator families at {tools.pin[:10]}:"]
        for row in rows[:_MAX_LISTED]:
            makes = "footprints" + (" + 3D" if row.models else "") if row.footprints else "3D"
            data = f"{len(row.data)} data files" if row.data else "no entry format (script)"
            lines.append(f"- {row.name}: {makes}; {data}")
        if len(rows) > _MAX_LISTED:
            lines.append(f"... {len(rows) - _MAX_LISTED} more; narrow with query.")
        return "\n".join(lines)

    @mcp.tool()
    @headless_compatible
    def lib_generate_footprint(
        family: str,
        entry: str,
        output_library: str = "",
        models: bool = True,
        datasheet: str = "",
    ) -> str:
        """Generate a footprint and its 3D body from a datasheet with a Library Tools family.

        ``entry`` is one YAML entry in the family's format (see lib_list_footprint_families
        with ``family``), with the dimensions from the datasheet's package drawing and
        recommended land pattern. The footprint goes to ``libs/<output_library>.pretty`` (the
        project's name by default) and the STEP body to ``libs/3d``, registered in the
        project's fp-lib-table. ``datasheet`` names the document and pages the values came
        from; it is kept in the footprint's record (libs/footprint-records.json). The first
        3D model may install CadQuery into the tools folder.
        """
        try:
            tools = _tools(models=models)
            generated = runner.generate(tools, family, entry, models=tools.models)
            return _filed(
                generated,
                library=output_library,
                rung=f"family {family}",
                datasheet=datasheet,
                source=entry,
                tools=tools,
            )
        except (install.LibraryToolsInstallError, runner.LibraryToolsError, OSError) as exc:
            return _failure(exc)

    @mcp.tool()
    @headless_compatible
    def lib_generate_footprint_custom(
        script: str, output_library: str = "", datasheet: str = ""
    ) -> str:
        """Generate a footprint for a part no family covers, from a script written for it.

        ``script`` is Python run in Library Tools' environment: it builds the footprint with
        KicadModTree (``from KicadModTree import *``; ``KicadFileHandler(fp).writeFile(...)``)
        under ``os.environ["KICAD_MCP_FOOTPRINT_DIR"]``, and may build a body with CadQuery
        (``cq.exporters.export(body, ...)``) as STEP under ``os.environ["KICAD_MCP_MODEL_DIR"]``
        with the footprint's name. The result is filed like lib_generate_footprint and marked
        custom: it is assigned only after the person accepts it.
        """
        try:
            tools = _tools(models=True)
            generated = runner.generate_custom(tools, script)
            return _filed(
                generated,
                library=output_library,
                rung="custom",
                datasheet=datasheet,
                source="custom script",
                tools=tools,
            )
        except (install.LibraryToolsInstallError, runner.LibraryToolsError, OSError) as exc:
            return _failure(exc)

    @mcp.tool()
    @headless_compatible
    def lib_check_footprint(
        path: str,
        land_pattern: list[dict[str, Any]] | None = None,
        datasheet_ref: str = "",
        tolerance_mm: float = 0.05,
    ) -> str:
        """Check a footprint against KiCad's library conventions and the datasheet's pads.

        ``path`` is a .kicad_mod in the project, or a library footprint as
        ``Library:Footprint`` (e.g. ``Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm``). Runs the
        KLC footprint checker. With ``land_pattern`` (the datasheet's recommended land
        pattern: ``[{number, x, y, w, h}]`` in mm, pad centres in the footprint's frame,
        origin at the package centre, y down), every pad is compared within ``tolerance_mm``
        and each mismatch is named. Passes only with no KLC error and no mismatch; KLC
        warnings are listed for judgement, and KLC naming rules (G1.1, F9.1, F9.3) are notes
        for a project library. The result is recorded with ``datasheet_ref``.
        """
        try:
            tools = _tools(models=False)
            target = _footprint_path(path)
            if not target.is_file():
                return f"Not done: {path} does not exist."
            pads = _pads(land_pattern or [])
            result = runner.check(tools, target, pads, tolerance_mm)
        except (
            install.LibraryToolsInstallError,
            runner.LibraryToolsError,
            OSError,
            ValueError,
        ) as exc:
            return _failure(exc)
        project_dir, _ = _project()
        runner.update_record(
            project_dir,
            target.stem,
            check=runner.check_record(result, datasheet_ref, tolerance_mm),
        )
        verdict = "PASS" if result.passed else "FAIL"
        lines = [*_notes(tools), f"{verdict}: {target.stem}"]
        if pads:
            lines.append(
                f"Datasheet pads compared: {result.compared} within {tolerance_mm:g} mm"
                + (f" ({datasheet_ref})" if datasheet_ref else "")
            )
        else:
            lines.append("No land pattern given: only the KLC checks ran.")
        lines += [f"- mismatch: {m}" for m in result.mismatches]
        lines += [f"- KLC error {e}" for e in result.klc_errors]
        lines += [f"- KLC warning {w}" for w in result.klc_warnings]
        lines += [f"- note {n}" for n in result.notes]
        return "\n".join(lines)
