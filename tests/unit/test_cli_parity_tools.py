"""Unit tests for CLI parity / export wrapper tools (FAZ 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.tools.export import _safe_output_filename
from kicad_mcp.tools.export_support import _run_cli_variants


def test_safe_output_filename_valid() -> None:
    name = _safe_output_filename("output.csv", default_name="out.csv")
    assert name == "output.csv"


def test_safe_output_filename_rejects_path() -> None:
    with pytest.raises(ValueError, match="directory"):
        _safe_output_filename("../escape.csv", default_name="out.csv")


def test_safe_output_filename_rejects_absolute() -> None:
    with pytest.raises(ValueError, match="directory separators"):
        _safe_output_filename("C:\\Windows\\file.csv", default_name="out.csv")


def test_safe_output_filename_empty_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        _safe_output_filename("  ", default_name="out.csv")


def test_run_cli_variants_missing_binary(fake_cli: Path) -> None:
    code, stdout, stderr = _run_cli_variants(
        [["--nonexistent"]],
    )
    # With fake_cli the shell script always exits 0, so code == 0
    # (the test validates the function runs without FileNotFoundError)
    assert isinstance(code, int)
    assert isinstance(stdout, str)


def test_an_absolute_path_in_the_workspace_is_written_there(
    sample_project: Path,
) -> None:
    from kicad_mcp.config import get_config
    from kicad_mcp.tools.export import _resolve_output_file

    get_config().apply_project(sample_project)
    target = sample_project / "renders" / "top.png"
    assert _resolve_output_file("3d", str(target), default_name="render.png") == target.resolve()
    assert target.parent.is_dir()


def test_an_absolute_path_elsewhere_keeps_its_name_in_the_output_folder(
    sample_project: Path, tmp_path: Path
) -> None:
    from kicad_mcp.config import get_config
    from kicad_mcp.tools.export import _resolve_output_file

    get_config().apply_project(sample_project)
    # A host that turned the file name into a path in its own folder (the project's parent).
    out = _resolve_output_file("3d", str(tmp_path / "board.step"), default_name="board.step")
    assert (out.parent.name, out.name) == ("3d", "board.step")
    assert out.resolve().is_relative_to(sample_project.resolve())


def test_a_relative_path_with_folders_is_still_refused(sample_project: Path) -> None:
    from kicad_mcp.config import get_config
    from kicad_mcp.tools.export import _resolve_output_file

    get_config().apply_project(sample_project)
    with pytest.raises(ValueError, match="directory"):
        _resolve_output_file("3d", "../escape.step", default_name="board.step")
