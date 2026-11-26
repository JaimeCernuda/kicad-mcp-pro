"""Shared KiCad CLI and export path helpers.

This module intentionally has no dependency on public tool modules.  Keeping
these helpers here lets validation, DFM, routing, and export code share the
same CLI behavior without creating import cycles between tool modules.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ..config import get_config
from ..utils import telemetry as otel

_CLI_RETRY_ATTEMPTS = 3
_CLI_RETRY_BASE_DELAY_SEC = 0.1
__all__ = ["_run_cli", "_run_cli_variants", "subprocess", "time"]


def _sanitize_cli_text(text: str) -> str:
    cfg = get_config()
    sanitized = text.replace(str(cfg.kicad_cli), "kicad-cli")
    if cfg.project_dir is not None:
        sanitized = sanitized.replace(str(cfg.project_dir), "<project>")
    return sanitized.strip()


def _timeout_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _run_cli(*args: str, timeout: float | None = None) -> tuple[int, str, str]:
    """Run kicad-cli with the supplied arguments.

    Only a run that exceeded its timeout is tried again; any other failure returns
    kicad-cli's own exit code and output on the first attempt.
    """
    cfg = get_config()
    command = otel.safe_cli_command(args)
    started = time.perf_counter()
    status = "error"
    return_code: int | None = None
    last_result = (1, "", "The kicad-cli command did not run.")
    with otel.cli_span(command) as span:
        try:
            if not cfg.kicad_cli.exists():
                raise FileNotFoundError(
                    "KiCad was not found in KICAD_CLI_PATH, PATH or the standard install "
                    "folders. If it is installed elsewhere, set kicad_cli; otherwise see "
                    "install-kicad."
                )

            for attempt in range(_CLI_RETRY_ATTEMPTS):
                timed_out = False
                try:
                    result = subprocess.run(
                        [str(cfg.kicad_cli), *args],
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=timeout or cfg.cli_timeout,
                        check=False,
                    )
                    last_result = (
                        result.returncode,
                        _sanitize_cli_text(result.stdout),
                        _sanitize_cli_text(result.stderr),
                    )
                except subprocess.TimeoutExpired as exc:
                    timed_out = True
                    output = _sanitize_cli_text(_timeout_output(exc.stderr))
                    last_result = (
                        124,
                        _sanitize_cli_text(_timeout_output(exc.stdout)),
                        f"The kicad-cli command timed out after {exc.timeout:g} s."
                        + (f"\n{output}" if output else ""),
                    )
                except OSError as exc:
                    last_result = (1, "", _sanitize_cli_text(str(exc)))

                code = last_result[0]
                return_code = code
                status = {0: "ok", 124: "timeout"}.get(code, "error")
                if not timed_out or attempt == _CLI_RETRY_ATTEMPTS - 1:
                    return last_result
                time.sleep(_CLI_RETRY_BASE_DELAY_SEC * (2**attempt))
            return last_result
        finally:
            otel.record_cli_invocation(command, status, time.perf_counter() - started)
            otel.finish_cli_span(span, status=status, return_code=return_code)


def _run_cli_variants(variants: list[list[str]]) -> tuple[int, str, str]:
    """Try multiple command variants and return the first success.

    When every variant fails, the result carries each attempt's command, exit code and
    output in the order tried, with the first attempt's exit code, so the error that
    explains the failure is not replaced by a later spelling's usage error.
    """
    failures: list[tuple[list[str], tuple[int, str, str]]] = []
    for variant in variants:
        try:
            result = _run_cli(*variant)
        except FileNotFoundError:
            raise
        except subprocess.TimeoutExpired:
            result = (124, "", "The kicad-cli command timed out.")
        if result[0] == 0:
            return result
        failures.append((variant, result))
    if not failures:
        return (1, "", "No CLI variants were attempted.")
    if len(failures) == 1:
        return failures[0][1]
    attempts = [
        f"kicad-cli {' '.join(variant)} -> exit code {code}: "
        f"{(stderr.strip() or stdout.strip() or '(no output)')}"
        for variant, (code, stdout, stderr) in failures
    ]
    return (failures[0][1][0], "", "\n".join(attempts))


def _get_pcb_file() -> Path:
    cfg = get_config()
    if cfg.pcb_file is None or not cfg.pcb_file.exists():
        raise ValueError(
            "No PCB file is configured. Call kicad_set_project() or set KICAD_MCP_PCB_FILE."
        )
    return cfg.pcb_file


def _get_sch_file() -> Path:
    cfg = get_config()
    if cfg.sch_file is None or not cfg.sch_file.exists():
        raise ValueError(
            "No schematic file is configured. Call kicad_set_project() or set KICAD_MCP_SCH_FILE."
        )
    return cfg.sch_file


def _ensure_output_dir(subdir: str | None = None) -> Path:
    return get_config().ensure_output_dir(subdir)
