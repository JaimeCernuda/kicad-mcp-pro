from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from kicad_mcp.errors import ToolFailedError


def _service_module() -> ModuleType:
    spec = importlib.util.find_spec("kicad_mcp.manufacturing.panelization")
    assert spec is not None, "Panelization service module must be extracted"
    return importlib.import_module("kicad_mcp.manufacturing.panelization")


def _service(tmp_path: Path):  # type: ignore[no-untyped-def]
    module = _service_module()
    pcb = tmp_path / "demo.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    calls: list[list[str]] = []

    def run(cmd: list[str]):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    service = module.PanelizationService(
        kikit_available=lambda: True,
        get_pcb_file=lambda: pcb,
        ensure_output_dir=lambda name: tmp_path / name,
        resolve_output_path=lambda path_text: (tmp_path / path_text).resolve(),
        run_command=run,
    )
    return service, calls, pcb


def test_panelization_validates_environment_and_arguments(tmp_path: Path) -> None:
    module = _service_module()
    service = module.PanelizationService(
        kikit_available=lambda: False,
        get_pcb_file=lambda: None,
        ensure_output_dir=lambda name: tmp_path / name,
        resolve_output_path=lambda path_text: tmp_path / path_text,
        run_command=lambda _cmd: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with pytest.raises(ToolFailedError, match=re.escape("KiKit is not installed")):
        service.panelize()

    service = module.PanelizationService(
        kikit_available=lambda: True,
        get_pcb_file=lambda: None,
        ensure_output_dir=lambda name: tmp_path / name,
        resolve_output_path=lambda path_text: tmp_path / path_text,
        run_command=lambda _cmd: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    with pytest.raises(
        ToolFailedError,
        match=re.escape("No PCB file is configured. Call kicad_set_project() first."),
    ):
        service.panelize()

    service, _calls, _pcb = _service(tmp_path)
    with pytest.raises(ToolFailedError, match=re.escape("Invalid layout 'radial'")):
        service.panelize(layout="radial")
    with pytest.raises(ToolFailedError, match=re.escape("rows and cols must both be >= 1.")):
        service.panelize(rows=0)


def test_grid_dry_run_preserves_command_and_output_contract(tmp_path: Path) -> None:
    service, calls, pcb = _service(tmp_path)

    result = service.panelize(
        layout="grid",
        rows=2,
        cols=3,
        spacing_mm=2.5,
        frame_width_mm=6.0,
    )

    panel = tmp_path / "panel" / "demo_panel_2x3.kicad_pcb"
    expected = [
        "kikit",
        "panelize",
        "--layout",
        "grid; rows: 2; cols: 3; space: 2.5mm",
        "--tabs",
        "fixed; width: 3mm; count: 1",
        "--cuts",
        "mousebites; drill: 0.5mm; spacing: 0.8mm",
        "--framing",
        "railstb; width: 6.0mm",
        "--post",
        "millRoundedCorner",
        str(pcb),
        str(panel),
    ]
    assert calls == []
    assert f"- Output: {panel}" in result
    assert f"- Command: {' '.join(expected)}" in result
    assert "Set dry_run=false and confirm=true" in result


def test_panelization_preserves_write_safety_gates(tmp_path: Path) -> None:
    service, calls, _pcb = _service(tmp_path)

    with pytest.raises(
        ToolFailedError, match=re.escape("Panelization requires explicit confirmation")
    ):
        service.panelize(dry_run=False, confirm=False)
    assert calls == []

    panel_file = tmp_path / "panel" / "demo_panel_2x2.kicad_pcb"
    panel_file.parent.mkdir(parents=True, exist_ok=True)
    panel_file.write_text("(kicad_pcb)", encoding="utf-8")

    with pytest.raises(
        ToolFailedError, match=re.escape("Refusing to overwrite an existing panel file")
    ):
        service.panelize(dry_run=False, confirm=True)
    assert calls == []


def test_panelization_preserves_os_execution_failure(tmp_path: Path) -> None:
    module = _service_module()
    pcb = tmp_path / "demo.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")

    def fail(_cmd: list[str]):  # type: ignore[no-untyped-def]
        raise OSError("runner unavailable")

    service = module.PanelizationService(
        kikit_available=lambda: True,
        get_pcb_file=lambda: pcb,
        ensure_output_dir=lambda name: tmp_path / name,
        resolve_output_path=lambda path_text: tmp_path / path_text,
        run_command=fail,
    )
    with pytest.raises(ToolFailedError, match=re.escape("Failed to run KiKit: runner unavailable")):
        service.panelize(dry_run=False, confirm=True)


def test_panelization_executes_variants_and_preserves_failures(tmp_path: Path) -> None:
    service, calls, _pcb = _service(tmp_path)

    mouse = service.panelize(layout="mousebites", dry_run=False, confirm=True)
    vcut = service.panelize(
        layout="vcut", output_path="custom/panel.kicad_pcb", dry_run=False, confirm=True
    )

    assert "Panel created:" in mouse
    assert "Layout: mousebites" in mouse
    assert "Layout: vcut" in vcut
    assert any("mousebites" in " ".join(cmd) for cmd in calls)
    assert any("vcuts" in " ".join(cmd) for cmd in calls)

    module = _service_module()
    pcb = tmp_path / "demo.kicad_pcb"
    failing = module.PanelizationService(
        kikit_available=lambda: True,
        get_pcb_file=lambda: pcb,
        ensure_output_dir=lambda name: tmp_path / name,
        resolve_output_path=lambda path_text: tmp_path / path_text,
        run_command=lambda _cmd: SimpleNamespace(returncode=2, stdout="", stderr="bad panel"),
    )
    with pytest.raises(
        ToolFailedError, match=re.escape("KiKit panelization failed (exit 2):\nbad panel")
    ):
        failing.panelize(dry_run=False, confirm=True)

    def timeout(_cmd: list[str]):  # type: ignore[no-untyped-def]
        raise module.PanelizationTimeoutError

    timed = module.PanelizationService(
        kikit_available=lambda: True,
        get_pcb_file=lambda: pcb,
        ensure_output_dir=lambda name: tmp_path / name,
        resolve_output_path=lambda path_text: tmp_path / path_text,
        run_command=timeout,
    )
    with pytest.raises(
        ToolFailedError, match=re.escape("KiKit panelization timed out after 120 seconds.")
    ):
        timed.panelize(dry_run=False, confirm=True)
