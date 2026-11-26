from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from kicad_mcp.errors import ToolFailedError


def _service_type() -> type[Any]:
    spec = importlib.util.find_spec("kicad_mcp.export.sch_python_bom")
    assert spec is not None, "schematic Python BOM export service module must be extracted"
    module = importlib.import_module("kicad_mcp.export.sch_python_bom")
    return module.ExportSchPythonBomService


class FakeResolver:
    def __init__(self, output: Path | Exception) -> None:
        self.output = output
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, subdir: str, raw_name: str, *, default_name: str) -> Path:
        self.calls.append((subdir, raw_name, default_name))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


class FakeCli:
    def __init__(self, result: tuple[int, str, str] = (0, "", "")) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> tuple[int, str, str]:
        self.calls.append(args)
        return self.result


def test_export_preserves_default_output_and_exact_cli_argv(tmp_path: Path) -> None:
    service_type = _service_type()
    sch = tmp_path / "demo.kicad_sch"
    out = tmp_path / "bom" / "bom.xml"
    resolver = FakeResolver(out)
    cli = FakeCli()
    service = service_type(
        get_sch_file=lambda: sch,
        resolve_output_file=resolver,
        run_cli=cli,
    )

    assert service.export() == f"Legacy Python BOM exported to {out}"
    assert resolver.calls == [("bom", "", "bom.xml")]
    assert cli.calls == [("sch", "export", "python-bom", "--output", str(out), str(sch))]


def test_export_preserves_explicit_output_name(tmp_path: Path) -> None:
    service_type = _service_type()
    out = tmp_path / "bom" / "custom.xml"
    resolver = FakeResolver(out)
    service = service_type(
        get_sch_file=lambda: tmp_path / "demo.kicad_sch",
        resolve_output_file=resolver,
        run_cli=FakeCli(),
    )

    assert service.export("custom.xml") == f"Legacy Python BOM exported to {out}"
    assert resolver.calls == [("bom", "custom.xml", "bom.xml")]


def test_export_preserves_invalid_output_path_message(tmp_path: Path) -> None:
    service_type = _service_type()
    cli = FakeCli()
    service = service_type(
        get_sch_file=lambda: tmp_path / "demo.kicad_sch",
        resolve_output_file=FakeResolver(ValueError("unsafe path")),
        run_cli=cli,
    )

    with pytest.raises(ToolFailedError, match=re.escape("Invalid output path: unsafe path")):
        service.export("../escape.xml")
    assert cli.calls == []


def test_export_preserves_failure_message_precedence(tmp_path: Path) -> None:
    service_type = _service_type()

    def service_for(result: tuple[int, str, str]):
        return service_type(
            get_sch_file=lambda: tmp_path / "demo.kicad_sch",
            resolve_output_file=FakeResolver(tmp_path / "bom.xml"),
            run_cli=FakeCli(result),
        )

    with pytest.raises(
        ToolFailedError, match=re.escape("Legacy Python BOM export failed: stderr detail")
    ):
        service_for((2, "stdout detail", "stderr detail")).export()
    with pytest.raises(
        ToolFailedError, match=re.escape("Legacy Python BOM export failed: stdout detail")
    ):
        service_for((2, "stdout detail", "")).export()
    with pytest.raises(
        ToolFailedError, match=re.escape("Legacy Python BOM export failed: unknown error")
    ):
        service_for((2, "", "")).export()
