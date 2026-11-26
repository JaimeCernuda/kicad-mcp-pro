"""IPC failures must never masquerade as PCB design results."""

from __future__ import annotations

import pytest

from kicad_mcp.errors import IpcDisconnectedError, KiCadBoardNotOpenError, ToolFailedError
from kicad_mcp.tools import pcb


@pytest.mark.parametrize(
    "error",
    [
        IpcDisconnectedError("Disconnected stale KiCad IPC endpoint"),
        KiCadBoardNotOpenError("No board open"),
    ],
)
def test_board_summary_without_a_board_fails_with_its_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(pcb, "_configured_board_file", lambda: None)

    with pytest.raises(ToolFailedError) as exc_info:
        pcb._file_backed_board_summary(error)

    text = str(exc_info.value)
    assert "no configured .kicad_pcb file" in text
    assert "0 footprint" not in text


def test_file_backed_summary_reports_source_and_environment_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    board_file = tmp_path / "test.kicad_pcb"
    board_file.write_text('(kicad_pcb (version 20221018) (generator "test"))')
    monkeypatch.setattr(pcb, "_configured_board_file", lambda: board_file)

    report = pcb._file_backed_board_summary(IpcDisconnectedError("stale IPC endpoint"))

    assert report.verdict == "WARN"
    assert report.metadata["source"] == "file-backed"
    assert report.metadata["available"] is True
    assert report.metadata["footprints"] == 0
    assert report.metadata["nets"] == 0
    assert report.failure_mode == "environment"
    assert report.retryable is True
    assert report.findings[0].failure_mode == "environment"
    assert "using file-backed" in report.text
