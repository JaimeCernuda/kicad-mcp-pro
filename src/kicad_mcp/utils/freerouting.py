"""Helpers for FreeRouting-based Specctra workflows."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from ..config import get_config
from ..discovery import get_cli_capabilities
from ..errors import ManualStepRequiredError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FreeRoutingResult:
    """Normalized outcome of a FreeRouting autoroute run."""

    mode: str
    command: tuple[str, ...]
    input_dsn: Path
    output_ses: Path
    returncode: int
    stdout: str
    stderr: str
    routed_pct: float
    total_nets: int
    unrouted_nets: list[str]
    pass_count: int
    wall_seconds: float
    stdout_tail: str
    ses_path: Path
    notes: tuple[str, ...] = ()  # what was installed to run FreeRouting here


def _sanitize_text(text: str) -> str:
    cfg = get_config()
    sanitized = text.replace(str(cfg.kicad_cli), "kicad-cli")
    if cfg.freerouting_jar is not None:
        sanitized = sanitized.replace(str(cfg.freerouting_jar), "<freerouting-jar>")
    if cfg.project_dir is not None:
        sanitized = sanitized.replace(str(cfg.project_dir), "<project>")
    return sanitized.strip()


def _common_parent(paths: list[Path]) -> Path:
    return Path(os.path.commonpath([str(path.resolve()) for path in paths]))


def _container_relpath(base: Path, target: Path) -> str:
    return target.resolve().relative_to(base.resolve()).as_posix()


def _tail(text: str, limit: int = 4096) -> str:
    return text[-limit:]


def _coerce_process_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _count_dsn_nets(dsn_path: Path) -> int:
    try:
        content = dsn_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    return len(re.findall(r"\(\s*net\b", content, flags=re.IGNORECASE))


def _parse_unrouted_nets(text: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(
        r"(?:unrouted|failed)\s+(?:net|connection)\s+['\"]?([A-Z0-9_.$:+/-]+)",
        text,
        flags=re.IGNORECASE,
    ):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def _parse_pass_count(text: str) -> int:
    passes = [
        int(match.group(1))
        for match in re.finditer(r"\bpass(?:es)?\s*+[:#]?\s*+(\d++)", text, flags=re.IGNORECASE)
    ]
    return max(passes) if passes else 0


def _parse_routed_pct(text: str, total_nets: int, unrouted_nets: list[str], ses_ok: bool) -> float:
    # Scan overlapping numeric starts and check the percent/label suffix literally.
    # Requiring the suffix within a regex permits super-linear backtracking.
    for number in re.finditer(r"(?=(?<!\d)(\d+(?:\.\d+)?))", text):
        cursor = number.start() + len(number.group(1))
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] != "%":
            continue
        cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        suffix = text[cursor : cursor + 9].casefold()
        if suffix.startswith(("routed", "completed")):
            return round(float(number.group(1)), 2)
    if total_nets > 0 and unrouted_nets:
        return round(((total_nets - len(unrouted_nets)) / total_nets) * 100.0, 2)
    return 100.0 if ses_ok else 0.0


def _docker_available(executable: str) -> bool:
    """Docker is installed and its daemon answers (an installed but stopped Docker is not)."""
    if shutil.which(executable) is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [executable, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


class FreeRoutingRunner:
    """Run FreeRouting against project-provided Specctra files."""

    def __init__(
        self,
        *,
        docker_image: str | None = None,
        docker_executable: str | None = None,
        java_executable: str | None = None,
    ) -> None:
        cfg = get_config()
        self._docker_image = docker_image or cfg.freerouting_image
        self._docker_executable = docker_executable or cfg.docker_executable
        self._java_executable = java_executable or cfg.java_executable
        self.install_notes: tuple[str, ...] = ()

    def _cli_export_dsn(self, kicad_cli: str | Path, pcb_path: Path, target: Path) -> bool:
        """Attempt the headless ``kicad-cli pcb export specctra`` command.

        Returns True only if a non-empty DSN file was produced. The capability probe
        gates the call, but the exact CLI build may still reject it, so failure is not
        fatal Ã¢â‚¬â€ the caller falls back to a manual-step path.
        """
        cli = str(kicad_cli)
        variants = [
            [cli, "pcb", "export", "specctra", "--output", str(target), str(pcb_path)],
            [cli, "pcb", "export", "specctra", "--input", str(pcb_path), "--output", str(target)],
        ]
        for cmd in variants:
            try:
                result = subprocess.run(  # noqa: S603
                    cmd, capture_output=True, text=True, timeout=120, check=False
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
                return True
        return False

    def _python_export_dsn(self, kicad_cli: str | Path, pcb_path: Path, target: Path) -> bool:
        """Export the DSN through KiCad's own Python module (``pcbnew.ExportSpecctraDSN``).

        KiCad 10's CLI has no Specctra export (KiCad 11 adds it), but its scripting module
        has had one for years. The interpreter is the one KiCad ships beside its CLI
        (Windows, macOS) or the system Python that has KiCad's module (Linux packages).
        Returns True only if a non-empty DSN file was produced.
        """
        script = (
            "import sys, pcbnew; "
            "b = pcbnew.LoadBoard(sys.argv[1]); "
            "sys.exit(0 if pcbnew.ExportSpecctraDSN(b, sys.argv[2]) else 1)"
        )
        for python in _kicad_pythons(kicad_cli):
            try:
                result = subprocess.run(  # noqa: S603
                    [str(python), "-c", script, str(pcb_path), str(target)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
                return True
        return False

    def export_dsn(self, pcb_path: Path, dsn_path: Path) -> Path:
        """Export a Specctra DSN for FreeRouting, or raise a clear manual-step error.

        Tries an already-staged file, then a headless ``kicad-cli pcb export specctra``
        when the CLI advertises it, then known sidecar locations. If none succeed it
        raises ``ManualStepRequiredError`` describing the one-time GUI export rather than
        dead-ending.
        """
        cfg = get_config()
        target = cfg.resolve_within_project(dsn_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            return target

        caps = get_cli_capabilities(cfg.kicad_cli)
        if caps.supports_specctra_export and self._cli_export_dsn(cfg.kicad_cli, pcb_path, target):
            return target
        if self._python_export_dsn(cfg.kicad_cli, pcb_path, target):
            return target

        candidates = [
            pcb_path.with_suffix(".dsn"),
            cfg.project_root / "routing" / f"{pcb_path.stem}.dsn",
            cfg.project_root / "output" / "routing" / f"{pcb_path.stem}.dsn",
        ]
        for candidate in candidates:
            if not candidate.exists():
                continue
            if candidate.resolve() != target.resolve():
                shutil.copy2(candidate, target)
            return target

        raise ManualStepRequiredError(
            "Headless Specctra DSN export is not available from this KiCad CLI "
            f"({cfg.kicad_cli}). In KiCad's PCB Editor run File > Export > Specctra DSN and "
            f"save it to {target} (or next to {pcb_path.name}), then retry."
        )

    def _build_docker_command(
        self,
        *,
        dsn_path: Path,
        output: Path,
        drc_output: Path | None,
        max_passes: int,
        thread_count: int,
        ignore_arg: str,
    ) -> list[str]:
        mount_paths = [dsn_path, output]
        if drc_output is not None:
            mount_paths.append(drc_output)
        mount_root = _common_parent(mount_paths)
        dsn_arg = _container_relpath(mount_root, dsn_path)
        ses_arg = _container_relpath(mount_root, output)
        drc_arg = _container_relpath(mount_root, drc_output) if drc_output is not None else None
        command = [
            self._docker_executable,
            "run",
            "--rm",
            "-v",
            f"{mount_root}:/work",
            "-w",
            "/work",
            self._docker_image,
            # The image's entrypoint runs its arguments as a command, and its default command
            # starts the API server, so the CLI run names the JAR itself.
            "java",
            "-jar",
            "/app/freerouting-executable.jar",
            "-de",
            dsn_arg,
            "-do",
            ses_arg,
            "-mp",
            str(max_passes),
            "-mt",
            str(thread_count),
            f"--router.max_passes={max_passes}",
        ]
        if ignore_arg:
            command.extend(["-inc", ignore_arg])
        if drc_arg is not None:
            command.extend(["-drc", drc_arg])
        return command

    def _build_jar_command(
        self,
        *,
        jar_path: Path | None,
        dsn_path: Path,
        output: Path,
        drc_output: Path | None,
        max_passes: int,
        thread_count: int,
        ignore_arg: str,
    ) -> list[str]:
        if jar_path is None:
            raise RuntimeError(
                "FreeRouting JAR path is required when use_docker=False. "
                "Set KICAD_MCP_FREEROUTING_JAR or pass freerouting_jar_path."
            )
        command = [
            self._java_executable,
            "-jar",
            str(jar_path),
            "-de",
            str(dsn_path.resolve()),
            "-do",
            str(output),
            "-mp",
            str(max_passes),
            "-mt",
            str(thread_count),
            f"--router.max_passes={max_passes}",
        ]
        if ignore_arg:
            command.extend(["-inc", ignore_arg])
        if drc_output is not None:
            command.extend(["-drc", str(drc_output)])
        return command

    def _resolve_command_and_mode(
        self,
        *,
        use_docker: bool,
        jar_path: Path | None,
        dsn_path: Path,
        output: Path,
        drc_output: Path | None,
        max_passes: int,
        thread_count: int,
        ignore_arg: str,
    ) -> tuple[str, list[str]]:
        # A configured JAR, else Docker when asked for and running, else FreeRouting and a
        # Java 21 runtime installed here, else Docker anyway, else how the user installs it.
        from ..routing import freerouting_install  # noqa: PLC0415 - keeps utils free of routing at import

        docker_ready = _docker_available(self._docker_executable)
        selected_docker = jar_path is None and use_docker and docker_ready
        java: str | None = None
        if not selected_docker and jar_path is None:  # a configured JAR runs as configured
            try:
                installed = freerouting_install.ensure(jar_path)
            except freerouting_install.FreeRoutingInstallError:
                if not docker_ready:
                    raise
                selected_docker = True
            else:
                jar_path, java = installed.jar, str(installed.java)
                self.install_notes = installed.notes

        if selected_docker:
            return "docker", self._build_docker_command(
                dsn_path=dsn_path,
                output=output,
                drc_output=drc_output,
                max_passes=max_passes,
                thread_count=thread_count,
                ignore_arg=ignore_arg,
            )
        command = self._build_jar_command(
            jar_path=jar_path,
            dsn_path=dsn_path,
            output=output,
            drc_output=drc_output,
            max_passes=max_passes,
            thread_count=thread_count,
            ignore_arg=ignore_arg,
        )
        if java is not None:
            command[0] = java
        return "jar", command

    def _execute_process(
        self,
        command: list[str],
        mode: str,
        dsn_path: Path,
        timeout: float,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        start_time = time.perf_counter()
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            missing = self._docker_executable if mode == "docker" else self._java_executable
            raise RuntimeError(
                f"{missing} was not found. Install the required runtime or switch "
                f"{'use_docker' if mode == 'docker' else 'to Docker mode'}."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            logger.warning(
                "freerouting_timeout",
                timeout_seconds=exc.timeout,
                dsn=dsn_path.name,
                stdout_tail=_tail(_sanitize_text(_coerce_process_output(exc.stdout))),
                stderr_tail=_tail(_sanitize_text(_coerce_process_output(exc.stderr))),
            )
            raise RuntimeError(
                f"FreeRouting timed out after {exc.timeout} seconds while "
                f"processing {dsn_path.name}."
            ) from exc
        wall_seconds = time.perf_counter() - start_time
        return result, wall_seconds

    def run_freerouting(
        self,
        dsn_path: Path,
        ses_path: Path,
        *,
        max_passes: int = 100,
        thread_count: int = 4,
        use_docker: bool = True,
        freerouting_jar_path: Path | None = None,
        net_classes_to_ignore: list[str] | None = None,
        exclude_nets: list[str] | None = None,
        drc_report_path: Path | None = None,
        timeout: float | None = None,
    ) -> FreeRoutingResult:
        """Run FreeRouting via Docker or a local JAR and return the normalized result."""
        cfg = get_config()
        if not dsn_path.exists():
            raise FileNotFoundError(f"Specctra DSN input was not found: {dsn_path}")

        output = ses_path.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        ignored = [*(net_classes_to_ignore or []), *(exclude_nets or [])]
        ignore_arg = ",".join(dict.fromkeys(ignored))
        drc_output = drc_report_path.resolve() if drc_report_path is not None else None
        if drc_output is not None:
            drc_output.parent.mkdir(parents=True, exist_ok=True)

        jar_path = freerouting_jar_path or cfg.freerouting_jar
        mode, command = self._resolve_command_and_mode(
            use_docker=use_docker,
            jar_path=jar_path,
            dsn_path=dsn_path,
            output=output,
            drc_output=drc_output,
            max_passes=max_passes,
            thread_count=thread_count,
            ignore_arg=ignore_arg,
        )

        effective_timeout = timeout or cfg.freerouting_timeout_sec
        result, wall_seconds = self._execute_process(command, mode, dsn_path, effective_timeout)

        sanitized_stdout = _sanitize_text(result.stdout)
        sanitized_stderr = _sanitize_text(result.stderr)
        combined_output = f"{sanitized_stdout}\n{sanitized_stderr}"
        total_nets = _count_dsn_nets(dsn_path)
        unrouted_nets = _parse_unrouted_nets(combined_output)
        ses_ok = output.exists() and output.stat().st_size > 0
        routed_pct = _parse_routed_pct(combined_output, total_nets, unrouted_nets, ses_ok)

        return FreeRoutingResult(
            mode=mode,
            command=tuple(str(part) for part in command),
            input_dsn=dsn_path.resolve(),
            output_ses=output,
            returncode=result.returncode,
            stdout=sanitized_stdout,
            stderr=sanitized_stderr,
            routed_pct=routed_pct,
            total_nets=total_nets,
            unrouted_nets=unrouted_nets,
            pass_count=_parse_pass_count(combined_output),
            wall_seconds=round(wall_seconds, 3),
            stdout_tail=_tail(sanitized_stdout),
            ses_path=output,
            notes=self.install_notes,
        )

    def stage_ses(self, ses_path: Path) -> Path:
        """Copy the routed SES session into the project routing output and return it."""
        cfg = get_config()
        if not ses_path.exists():
            raise FileNotFoundError(f"Specctra SES session was not found: {ses_path}")
        staged = cfg.resolve_within_project(cfg.ensure_output_dir("routing") / ses_path.name)
        if staged.resolve() != ses_path.resolve():
            shutil.copy2(ses_path, staged)
        return staged

    def import_ses(self, pcb_path: Path, ses_path: Path) -> Path:
        """Stage the SES session, then raise the manual-import step it still requires.

        KiCad exposes no headless path to apply a routed Specctra SES session to a board,
        so this never silently "succeeds": it stages the file and raises
        ``ManualStepRequiredError`` describing the one-time GUI import. The staged path is
        in the message so the caller can surface exactly where the session is.
        """
        _ = pcb_path
        staged = self.stage_ses(ses_path)
        raise ManualStepRequiredError(
            f"The routed session is staged at {staged}, but KiCad has no headless Specctra "
            "SES import. Open the PCB in KiCad's PCB Editor and run File > Import > Specctra "
            "Session to apply the routing, then save the board."
        )


def _kicad_pythons(kicad_cli: str | Path) -> list[Path]:
    """Interpreters that can import KiCad's ``pcbnew`` module, the CLI's own first."""
    cli = Path(str(kicad_cli))
    if not cli.is_absolute():
        cli = Path(shutil.which(str(cli)) or cli)
    folder = cli.resolve().parent if cli.exists() else cli.parent
    found = [
        folder / "python.exe",  # Windows: KiCad's bin folder
        folder / "python3",
        folder.parent
        / "Frameworks"
        / "Python.framework"
        / "Versions"
        / "Current"
        / "bin"
        / "python3",
    ]
    # A server run from its own virtual environment finds that environment's python3 first;
    # Linux packages put KiCad's module in the system Python.
    for name in (shutil.which("python3"), "/usr/bin/python3"):
        if name and Path(name) not in found:
            found.append(Path(name))
    return [path for path in found if path.exists()]


def import_ses_with_kicad(pcb_text: str, ses_path: Path, kicad_cli: str | Path) -> str:
    """``pcb_text`` with ``ses_path``'s routing applied by KiCad's own Python module.

    The board text is written to a scratch file, loaded with ``pcbnew.LoadBoard``, routed
    with ``pcbnew.ImportSpecctraSES`` and saved, so KiCad itself writes the tracks and vias
    in its current format (named nets included). Raises ``ValueError`` when no KiCad Python
    can do it.
    """
    script = (
        "import sys, pcbnew; "
        "b = pcbnew.LoadBoard(sys.argv[1]); "
        "ok = pcbnew.ImportSpecctraSES(b, sys.argv[2]); "
        "ok = ok and b.Save(sys.argv[1]); "
        "sys.exit(0 if ok else 1)"
    )
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="kicad-ses-") as scratch:
        board = Path(scratch) / "board.kicad_pcb"
        board.write_text(pcb_text, encoding="utf-8")
        errors: list[str] = []
        for python in _kicad_pythons(kicad_cli):
            try:
                result = subprocess.run(  # noqa: S603
                    [str(python), "-c", script, str(board), str(ses_path.resolve())],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{python}: {exc}")
                continue
            if result.returncode == 0:
                return board.read_text(encoding="utf-8")
            errors.append(f"{python}: {(result.stderr or result.stdout).strip()[-200:]}")
    raise ValueError(
        "KiCad's Python module could not import the SES ("
        + ("; ".join(errors) or "no KiCad Python found")
        + ")."
    )
