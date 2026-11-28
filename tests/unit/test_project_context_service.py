from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from kicad_mcp.errors import ToolFailedError


def _service_type() -> type[Any]:
    try:
        spec = importlib.util.find_spec("kicad_mcp.project.context")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "Project context service module must be extracted"
    module = importlib.import_module("kicad_mcp.project.context")
    service_type = getattr(module, "ProjectContextService", None)
    assert service_type is not None, "ProjectContextService must own project activation behavior"
    return service_type


def test_set_project_applies_scanned_paths_and_runtime_resets(tmp_path: Path) -> None:
    project_file = tmp_path / "demo.kicad_pro"
    pcb_file = tmp_path / "demo.kicad_pcb"
    sch_file = tmp_path / "demo.kicad_sch"
    for path in (project_file, pcb_file, sch_file):
        path.write_text("", encoding="utf-8")

    applied: list[tuple[Path, dict[str, Path | None]]] = []
    events: list[str] = []
    service_type = _service_type()
    service = service_type(
        scan_project_dir=lambda path: {
            "project": project_file,
            "pcb": pcb_file,
            "schematic": sch_file,
        },
        apply_project=lambda project_dir, **kwargs: applied.append((project_dir, kwargs)),
        clear_cache=lambda: events.append("cache"),
        reset_connection=lambda: events.append("connection"),
        current_pcb_file=lambda: None,
        release_live_edit=lambda same: events.append(f"live-edit:{same}") or "",
        render_project_info=lambda: "project-info",
    )

    result = service.set_project(str(tmp_path))

    assert result == "project-info"
    assert applied == [
        (
            tmp_path.resolve(),
            {
                "project_file": project_file,
                "pcb_file": pcb_file,
                "sch_file": sch_file,
                "output_dir": tmp_path.resolve() / "output",
            },
        )
    ]
    assert events == ["live-edit:False", "cache", "connection"]


def test_reselecting_the_same_board_keeps_the_connection_and_reports_the_drop(
    tmp_path: Path,
) -> None:
    pcb_file = tmp_path / "demo.kicad_pcb"
    pcb_file.write_text("", encoding="utf-8")
    events: list[str] = []
    service = _service_type()(
        scan_project_dir=lambda _path: {"project": None, "pcb": pcb_file, "schematic": None},
        apply_project=lambda *_args, **_kwargs: None,
        clear_cache=lambda: events.append("cache"),
        reset_connection=lambda: events.append("connection"),
        current_pcb_file=lambda: pcb_file,
        release_live_edit=lambda same: (
            events.append(f"live-edit:{same}")
            or " This session's open KiCad commit was dropped with its staged changes."
        ),
        render_project_info=lambda: "project-info",
    )

    result = service.set_project(str(tmp_path))

    assert events == ["live-edit:True", "cache"]
    assert result == (
        "project-info\nThis session's open KiCad commit was dropped with its staged changes."
    )


def test_set_project_preserves_explicit_file_and_output_overrides(tmp_path: Path) -> None:
    explicit_pcb = tmp_path / "custom.kicad_pcb"
    explicit_sch = tmp_path / "custom.kicad_sch"
    explicit_output = tmp_path / "artifacts"
    applied: list[tuple[Path, dict[str, Path | None]]] = []
    service_type = _service_type()
    service = service_type(
        scan_project_dir=lambda _path: {
            "project": tmp_path / "demo.kicad_pro",
            "pcb": None,
            "schematic": None,
        },
        apply_project=lambda project_dir, **kwargs: applied.append((project_dir, kwargs)),
        clear_cache=lambda: None,
        reset_connection=lambda: None,
        current_pcb_file=lambda: None,
        release_live_edit=lambda _same: "",
        render_project_info=lambda: "project-info",
    )

    result = service.set_project(
        str(tmp_path),
        pcb_file=str(explicit_pcb),
        sch_file=str(explicit_sch),
        output_dir=str(explicit_output),
    )

    assert result == "project-info"
    assert applied[0][1] == {
        "project_file": tmp_path / "demo.kicad_pro",
        "pcb_file": explicit_pcb.resolve(),
        "sch_file": explicit_sch.resolve(),
        "output_dir": explicit_output.resolve(),
    }


def test_set_project_rejects_missing_directory_without_side_effects(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    events: list[str] = []
    service_type = _service_type()
    service = service_type(
        scan_project_dir=lambda _path: (_ for _ in ()).throw(AssertionError("scan must not run")),
        apply_project=lambda *_args, **_kwargs: events.append("apply"),
        clear_cache=lambda: events.append("cache"),
        reset_connection=lambda: events.append("connection"),
        current_pcb_file=lambda: None,
        release_live_edit=lambda same: events.append(f"live-edit:{same}") or "",
        render_project_info=lambda: "project-info",
    )

    with pytest.raises(
        ToolFailedError,
        match=re.escape("Project directory does not exist or is not a directory."),
    ):
        service.set_project(str(missing))
    assert events == []


def test_set_project_preserves_incomplete_project_error(tmp_path: Path) -> None:
    project_file = tmp_path / "demo.kicad_pro"
    project_file.write_text("{}", encoding="utf-8")
    service_type = _service_type()
    service = service_type(
        scan_project_dir=lambda _path: {"project": project_file, "pcb": None, "schematic": None},
        apply_project=lambda *_args, **_kwargs: None,
        clear_cache=lambda: None,
        reset_connection=lambda: None,
        current_pcb_file=lambda: None,
        release_live_edit=lambda _same: "",
        render_project_info=lambda: "project-info",
    )

    with pytest.raises(
        ToolFailedError,
        match=re.escape(
            "E_PROJECT_SCAN_INCOMPLETE: Found a .kicad_pro file but no matching "
            ".kicad_pcb or .kicad_sch file in the selected directory. "
            "Add at least one board or schematic file before activating this project."
        ),
    ):
        service.set_project(str(tmp_path))


def test_get_project_info_delegates_to_renderer() -> None:
    service_type = _service_type()
    service = service_type(
        scan_project_dir=lambda _path: {},
        apply_project=lambda *_args, **_kwargs: None,
        clear_cache=lambda: None,
        reset_connection=lambda: None,
        current_pcb_file=lambda: None,
        release_live_edit=lambda _same: "",
        render_project_info=lambda: "current-project-info",
    )

    assert service.get_project_info() == "current-project-info"


def test_relative_project_file_overrides_resolve_inside_selected_project(tmp_path: Path) -> None:
    root = tmp_path / "board_a"
    root.mkdir()
    (root / "my.kicad_pcb").write_text("", encoding="utf-8")
    (root / "my.kicad_sch").write_text("", encoding="utf-8")
    captured: list[dict[str, Path | None]] = []
    service = _service_type()(
        scan_project_dir=lambda _path: {"project": None, "pcb": None, "schematic": None},
        apply_project=lambda _project_dir, **kwargs: captured.append(kwargs),
        clear_cache=lambda: None,
        reset_connection=lambda: None,
        current_pcb_file=lambda: None,
        release_live_edit=lambda _same: "",
        render_project_info=lambda: "ready",
    )
    assert (
        service.set_project(str(root), pcb_file="my.kicad_pcb", sch_file="my.kicad_sch") == "ready"
    )
    assert captured[0]["pcb_file"] == root / "my.kicad_pcb"
    assert captured[0]["sch_file"] == root / "my.kicad_sch"


def test_explicit_project_files_cannot_escape_into_other_project(tmp_path: Path) -> None:
    import pytest

    from kicad_mcp.path_safety import UnsafePathError

    first = tmp_path / "board_a"
    second = tmp_path / "board_b"
    first.mkdir()
    second.mkdir()
    (second / "board.kicad_pcb").write_text("", encoding="utf-8")
    changes: list[str] = []
    service = _service_type()(
        scan_project_dir=lambda _path: {"project": None, "pcb": None, "schematic": None},
        apply_project=lambda *_args, **_kwargs: changes.append("applied"),
        clear_cache=lambda: changes.append("cache"),
        reset_connection=lambda: changes.append("connection"),
        current_pcb_file=lambda: None,
        release_live_edit=lambda _same: changes.append("edit") or "",
        render_project_info=lambda: "ready",
    )
    for path in ("../board_b/board.kicad_pcb", str(second / "board.kicad_pcb")):
        with pytest.raises(UnsafePathError):
            service.set_project(str(first), pcb_file=path)
    (first / "linked.kicad_pcb").symlink_to(second / "board.kicad_pcb")
    with pytest.raises(UnsafePathError):
        service.set_project(str(first), pcb_file="linked.kicad_pcb")
    assert changes == []
