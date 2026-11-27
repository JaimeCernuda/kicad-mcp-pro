from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.errors import ManualStepRequiredError
from kicad_mcp.routing.specctra_staging import (
    RoutingSpecctraStagingService,
    relative_project_path,
)


class FakeRunner:
    def __init__(
        self,
        *,
        exported: Path | Exception | None = None,
        staged: Path | Exception | None = None,
    ) -> None:
        self.exported = exported
        self.staged = staged
        self.export_calls: list[tuple[Path, Path]] = []
        self.stage_calls: list[Path] = []

    def export_dsn(self, pcb_path: Path, dsn_path: Path) -> Path:
        self.export_calls.append((pcb_path, dsn_path))
        if isinstance(self.exported, Exception):
            raise self.exported
        assert isinstance(self.exported, Path)
        return self.exported

    def stage_ses(self, ses_path: Path) -> Path:
        self.stage_calls.append(ses_path)
        if isinstance(self.staged, Exception):
            raise self.staged
        assert isinstance(self.staged, Path)
        return self.staged


def _service(
    project_root: Path,
    runner: FakeRunner,
    *,
    pcb_file: Path | None = None,
) -> RoutingSpecctraStagingService:
    board = pcb_file or project_root / "demo.kicad_pcb"
    return RoutingSpecctraStagingService(
        runner_factory=lambda: runner,
        get_pcb_file=lambda: board,
        resolve_within_project=lambda path: project_root / Path(path),
        get_project_root=lambda: project_root,
    )


def test_export_dsn_preserves_success_result_contract(tmp_path: Path) -> None:
    dsn = tmp_path / "output" / "routing" / "board.dsn"
    runner = FakeRunner(exported=dsn)
    service = _service(tmp_path, runner)

    result = service.export_dsn()

    assert runner.export_calls == [(tmp_path / "demo.kicad_pcb", Path("output/routing/board.dsn"))]
    assert result.ok is True
    assert result.changed is True
    assert result.human_gate_required is False
    assert result.tool_name == "route_export_dsn"
    assert [(artifact.path, artifact.kind) for artifact in result.artifacts] == [(str(dsn), "dsn")]
    relative_dsn = str(Path("output") / "routing" / "board.dsn")
    assert result.state_delta.summary == (
        f"Specctra DSN ready at {relative_dsn}. "
        "You can route it with route_autoroute_freerouting()."
    )
    assert result.state_delta.changed_files == [str(dsn)]


def test_export_dsn_preserves_manual_gate_result(tmp_path: Path) -> None:
    runner = FakeRunner(exported=ManualStepRequiredError("manual DSN export required"))
    result = _service(tmp_path, runner).export_dsn()

    assert result.ok is False
    assert result.changed is False
    assert result.human_gate_required is True
    assert result.errors == ["manual DSN export required"]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("runner failed"), ValueError("invalid target")],
)
def test_export_dsn_preserves_operational_failure_text(
    tmp_path: Path,
    error: Exception,
) -> None:
    runner = FakeRunner(exported=error)
    result = _service(tmp_path, runner).export_dsn()

    assert result.ok is False
    assert result.errors == [f"Specctra DSN export is unavailable: {error}"]


def test_import_ses_preserves_staging_and_human_gate_contract(tmp_path: Path) -> None:
    staged = tmp_path / "output" / "routing" / "board.ses"
    runner = FakeRunner(staged=staged)
    service = _service(tmp_path, runner)

    result = service.import_ses()

    assert runner.stage_calls == [tmp_path / "output" / "routing" / "board.ses"]
    assert result.ok is True
    assert result.changed is False
    assert result.human_gate_required is True
    assert result.tool_name == "route_import_ses"
    assert [(artifact.path, artifact.kind) for artifact in result.artifacts] == [
        (str(staged), "ses")
    ]
    relative_ses = str(Path("output") / "routing" / "board.ses")
    assert result.state_delta.summary == (
        f"Specctra SES session staged at {relative_ses}, but it could not be applied "
        "headlessly (no headless applier is configured): open the PCB Editor and run "
        "File > Import > Specctra Session to apply the routing, then save."
    )
    assert result.state_delta.changed_files == [str(staged)]


def test_import_ses_applies_the_session_headlessly(tmp_path: Path) -> None:
    """Found live (KQ32 fork): "KiCad has no headless SES import" made an agent abandon
    FreeRouting and write its own router; KiCad's Python applies the session."""
    staged = tmp_path / "output" / "routing" / "board.ses"
    board = tmp_path / "demo.kicad_pcb"
    applied: list[Path] = []

    def apply(path: Path) -> tuple[Path, int, int]:
        applied.append(path)
        return board, 231, 12

    service = _service(tmp_path, FakeRunner(staged=staged))
    service = RoutingSpecctraStagingService(
        runner_factory=service.runner_factory,
        get_pcb_file=service.get_pcb_file,
        resolve_within_project=service.resolve_within_project,
        get_project_root=service.get_project_root,
        apply_ses=apply,
    )
    result = service.import_ses()
    assert applied == [staged]
    assert result.ok and result.changed and not result.human_gate_required
    assert "231 track segments and 12 vias" in result.state_delta.summary
    assert result.state_delta.changed_files == [str(board)]


def test_import_ses_names_why_it_could_not_apply(tmp_path: Path) -> None:
    def apply(_path: Path) -> tuple[Path, int, int]:
        raise ValueError("KiCad's Python module could not import the SES (no KiCad Python)")

    base = _service(tmp_path, FakeRunner(staged=tmp_path / "board.ses"))
    service = RoutingSpecctraStagingService(
        runner_factory=base.runner_factory,
        get_pcb_file=base.get_pcb_file,
        resolve_within_project=base.resolve_within_project,
        get_project_root=base.get_project_root,
        apply_ses=apply,
    )
    result = service.import_ses()
    assert result.human_gate_required and not result.changed
    assert "no KiCad Python" in result.state_delta.summary


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing session"),
        RuntimeError("staging failed"),
        ValueError("invalid path"),
    ],
)
def test_import_ses_preserves_failure_text(tmp_path: Path, error: Exception) -> None:
    runner = FakeRunner(staged=error)
    result = _service(tmp_path, runner).import_ses()

    assert result.ok is False
    assert result.errors == [f"Specctra SES staging failed: {error}"]


def test_relative_project_path_preserves_external_absolute_path(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.ses"

    rendered = relative_project_path(outside, tmp_path)

    assert rendered == str(outside.resolve())
