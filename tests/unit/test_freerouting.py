from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kicad_mcp.errors import ManualStepRequiredError
from kicad_mcp.utils.freerouting import FreeRoutingRunner


def test_export_dsn_copies_existing_sibling_dsn(sample_project: Path) -> None:
    pcb_path = sample_project / "demo.kicad_pcb"
    source_dsn = sample_project / "demo.dsn"
    source_dsn.write_text("dsn", encoding="utf-8")

    runner = FreeRoutingRunner()
    staged = runner.export_dsn(pcb_path, Path("output/routing/board.dsn"))

    assert staged.exists()
    assert staged.read_text(encoding="utf-8") == "dsn"


def test_export_dsn_requires_manual_export_when_cli_lacks_specctra(sample_project: Path) -> None:
    runner = FreeRoutingRunner()

    with pytest.raises(ManualStepRequiredError) as exc_info:
        runner.export_dsn(sample_project / "demo.kicad_pcb", Path("output/routing/board.dsn"))

    assert "Specctra DSN" in str(exc_info.value)
    assert exc_info.value.code == "MANUAL_STEP_REQUIRED"


def test_run_freerouting_docker_builds_expected_command(
    sample_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn_path = sample_project / "board.dsn"
    ses_path = sample_project / "board.ses"
    dsn_path.write_text("dsn", encoding="utf-8")
    observed: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout, check, **kwargs: object):
        _ = (capture_output, text, timeout, check)
        _ = kwargs
        observed.append(cmd)
        ses_path.write_text("ses", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="pass 3\n100% routed\nok", stderr="")

    monkeypatch.setattr("kicad_mcp.utils.freerouting.subprocess.run", fake_run)
    monkeypatch.setattr("kicad_mcp.utils.freerouting._docker_available", lambda _: True)

    result = FreeRoutingRunner().run_freerouting(
        dsn_path,
        ses_path,
        max_passes=55,
        thread_count=6,
        use_docker=True,
        net_classes_to_ignore=["GND", "PWR"],
        exclude_nets=["SHIELD"],
        drc_report_path=sample_project / "freerouting.drc.json",
    )

    assert result.returncode == 0
    assert result.routed_pct == 100.0
    assert result.total_nets == 0
    assert result.pass_count == 3
    assert result.ses_path == ses_path.resolve()
    assert observed
    assert observed[0][:3] == ["docker", "run", "--rm"]
    assert "-de" in observed[0]
    assert "-do" in observed[0]
    assert "-mt" in observed[0]
    assert "--router.max_passes=55" in observed[0]
    assert "-inc" in observed[0]
    assert "GND,PWR,SHIELD" in observed[0]
    assert "-drc" in observed[0]


def test_run_freerouting_falls_back_to_jar_when_docker_missing(
    sample_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn_path = sample_project / "board.dsn"
    ses_path = sample_project / "board.ses"
    jar_path = sample_project / "freerouting.jar"
    dsn_path.write_text("(pcb (net A) (net B))", encoding="utf-8")
    jar_path.write_text("jar", encoding="utf-8")
    observed: list[list[str]] = []

    monkeypatch.setattr("kicad_mcp.utils.freerouting.shutil.which", lambda _: None)

    def fake_run(cmd, capture_output, text, timeout, check, **kwargs: object):
        _ = (capture_output, text, timeout, check)
        _ = kwargs
        observed.append(cmd)
        ses_path.write_text("ses", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="unrouted net B", stderr="")

    monkeypatch.setattr("kicad_mcp.utils.freerouting.subprocess.run", fake_run)

    result = FreeRoutingRunner().run_freerouting(
        dsn_path,
        ses_path,
        use_docker=True,
        freerouting_jar_path=jar_path,
    )

    assert observed[0][:2] == ["java", "-jar"]
    assert result.mode == "jar"
    assert result.total_nets == 2
    assert result.unrouted_nets == ["B"]
    assert result.routed_pct == 50.0


def test_without_docker_or_a_jar_the_error_says_how_to_install(
    sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When FreeRouting cannot be installed here (and Docker is not running), the user is
    told how to install it."""
    from kicad_mcp.routing import freerouting_install

    dsn_path = sample_project / "board.dsn"
    dsn_path.write_text("dsn", encoding="utf-8")

    def cannot(_jar: object = None) -> object:
        raise freerouting_install.FreeRoutingInstallError("No network here.")

    monkeypatch.setattr(freerouting_install, "ensure", cannot)
    monkeypatch.setattr("kicad_mcp.utils.freerouting._docker_available", lambda _exe: False)
    with pytest.raises(freerouting_install.FreeRoutingInstallError) as exc_info:
        FreeRoutingRunner().run_freerouting(dsn_path, sample_project / "b.ses", use_docker=False)
    message = str(exc_info.value)
    assert "No network here." in message
    assert "KICAD_MCP_FREEROUTING_JAR" in message and "Java 21" in message


def test_without_docker_the_installed_jar_and_java_run(
    sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp.routing import freerouting_install

    dsn_path = sample_project / "board.dsn"
    ses_path = sample_project / "board.ses"
    dsn_path.write_text("(pcb (net A))", encoding="utf-8")
    jar = sample_project / "fr.jar"
    java = sample_project / "jre" / "bin" / "java"
    installed = freerouting_install.Installed(jar=jar, java=java, notes=("Installed.",))
    monkeypatch.setattr(freerouting_install, "ensure", lambda _jar=None: installed)
    monkeypatch.setattr("kicad_mcp.utils.freerouting._docker_available", lambda _exe: False)
    observed: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(cmd)
        ses_path.write_text("ses", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("kicad_mcp.utils.freerouting.subprocess.run", fake_run)
    runner = FreeRoutingRunner()
    result = runner.run_freerouting(dsn_path, ses_path, use_docker=True)
    assert result.mode == "jar"
    assert observed[0][:3] == [str(java), "-jar", str(jar)]
    assert runner.install_notes == ("Installed.",)


def test_stage_ses_copies_session(sample_project: Path, tmp_path: Path) -> None:
    ses_path = tmp_path / "board.ses"
    ses_path.write_text("ses", encoding="utf-8")

    staged = FreeRoutingRunner().stage_ses(ses_path)

    assert staged.exists()
    assert staged.read_text(encoding="utf-8") == "ses"
    assert staged.parent.name == "routing"


def test_import_ses_stages_then_requires_manual_gui_step(
    sample_project: Path, tmp_path: Path
) -> None:
    ses_path = tmp_path / "board.ses"
    ses_path.write_text("ses", encoding="utf-8")

    # The session is staged (a real action), but applying it has no headless path, so
    # import_ses must never silently succeed: it raises a clear manual-step error.
    runner = FreeRoutingRunner()
    pcb_path = sample_project / "demo.kicad_pcb"
    with pytest.raises(ManualStepRequiredError) as exc_info:
        runner.import_ses(pcb_path, ses_path)

    assert "File > Import > Specctra Session" in str(exc_info.value)
    staged = sample_project / "output" / "routing" / "board.ses"
    assert staged.exists()


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("pass 3: 100% routed", 100.0),
        ("Pass 11\n 97.25 % completed", 97.25),
        ("input (100 nets) 42%ROUTED", 42.0),
        ("1.2.3%routed", 2.3),
        ("9" * 12_000 + "not-a-percent\n12.5% completed", 12.5),
    ],
)
def test_routed_percent_parser_matches_freerouting_output_without_backtracking(
    output: str, expected: float
) -> None:
    from kicad_mcp.utils.freerouting import _parse_routed_pct

    assert _parse_routed_pct(output, 0, [], (None, None)) == expected


def test_routed_percent_parser_retains_fallback_on_unrecognized_status() -> None:
    from kicad_mcp.utils.freerouting import _parse_routed_pct

    assert _parse_routed_pct("25% negotiating", 8, ["N1", "N2"]) == 75.0


def test_a_written_session_alone_is_not_reported_as_fully_routed() -> None:
    """Found live: the summary said "Routed: 100.00% (25 net(s), 0 unrouted)" because a
    session file was written, while FreeRouting's own report said one connection was open."""
    from kicad_mcp.utils.freerouting import _parse_routed_pct

    assert _parse_routed_pct("no percent", 0, []) is None


# FreeRouting 2.1's own report at the end of a run (abridged from the live sensor-board run).
_FREEROUTING_2_TAIL = """\
2026-11-15 23:31:45.013 INFO   [E986F7\\8E2213] Auto-router pass #806 on board '28634c' was \
completed in 0.03 seconds with the score of 985.14 (1 unrouted), using 1013.73 CPU seconds.
{
  "connections": {
    "maximum_count": 189,
    "incomplete_count": 1
  },
  "clearance_violations": {"total_count": 0}
}"""


def test_freerouting_reports_the_connections_it_left_open() -> None:
    from kicad_mcp.utils.freerouting import _parse_connections, _parse_routed_pct

    connections = _parse_connections(_FREEROUTING_2_TAIL)
    assert connections == (1, 189)
    assert _parse_routed_pct(_FREEROUTING_2_TAIL, 25, [], connections) == 99.47


def test_the_last_pass_line_counts_when_there_is_no_final_report() -> None:
    from kicad_mcp.utils.freerouting import _parse_connections

    text = "pass #1 ... (7 unrouted)\npass #2 ... (2 unrouted)\n"
    assert _parse_connections(text) == (2, None)


def test_the_routed_line_says_what_is_still_open(tmp_path: Path) -> None:
    from kicad_mcp.utils.freerouting import FreeRoutingResult

    def result(**kw: object) -> FreeRoutingResult:
        base: dict[str, object] = {
            "mode": "jar",
            "command": (),
            "input_dsn": tmp_path,
            "output_ses": tmp_path,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "routed_pct": None,
            "total_nets": 25,
            "unrouted_nets": [],
            "pass_count": 1,
            "wall_seconds": 1.0,
            "stdout_tail": "",
            "ses_path": tmp_path,
        }
        base.update(kw)
        return FreeRoutingResult(**base)  # type: ignore[arg-type]

    assert result(routed_pct=99.47, open_connections=1, total_connections=189).routed_line() == (
        "Routed: 99.47% of connections (1 of 189 still open)"
    )
    assert "did not report" in result().routed_line()
    assert "0 unrouted" not in result().routed_line()


def test_the_docker_run_names_the_jar_the_image_runs() -> None:
    """The image's entrypoint runs its arguments as a command; its default command is the
    API server, so a CLI run must name the JAR (or it fails: 'exec: -d: invalid option')."""
    from pathlib import Path

    from kicad_mcp.utils.freerouting import FreeRoutingRunner

    command = FreeRoutingRunner()._build_docker_command(
        dsn_path=Path("/w/board.dsn"),
        output=Path("/w/board.ses"),
        drc_output=None,
        max_passes=5,
        thread_count=1,
        ignore_arg="",
    )
    image = command.index(FreeRoutingRunner()._docker_image)
    assert command[image + 1 : image + 4] == ["java", "-jar", "/app/freerouting-executable.jar"]
    assert command[image + 4] == "-de"


def test_kicad_10_exports_the_dsn_through_its_python_module(monkeypatch, tmp_path) -> None:
    """KiCad 10's CLI has no Specctra export; KiCad's own Python module does."""
    import subprocess
    from pathlib import Path

    from kicad_mcp.utils import freerouting

    target = tmp_path / "board.dsn"
    python = tmp_path / "python3"
    python.write_text("")
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        target.write_text("(pcb board)")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(freerouting, "_kicad_pythons", lambda _cli: [python])
    monkeypatch.setattr(freerouting.subprocess, "run", run)
    runner = freerouting.FreeRoutingRunner()
    assert runner._python_export_dsn("/usr/bin/kicad-cli", Path("b.kicad_pcb"), target)
    assert calls[0][0] == str(python) and "ExportSpecctraDSN" in calls[0][2]


def test_a_named_net_board_takes_the_routes_through_kicads_own_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """KiCad 10 names nets on pads and copper; the fork's writer refuses that format, so
    KiCad's own SES import applies the routing instead of leaving a manual GUI step."""
    import anyio

    from kicad_mcp.routing import autorouter
    from kicad_mcp.utils import freerouting

    board = tmp_path / "b.kicad_pcb"
    board.write_text('(kicad_pcb (footprint "R" (pad "1" smd (net "GND"))))', encoding="utf-8")
    ses = tmp_path / "b.ses"
    ses.write_text(
        "(session b (routes (network_out (net GND (wire (path F.Cu 2000 0 0 10000 0))))))",
        encoding="utf-8",
    )
    calls: list[Path] = []

    def fake_import(pcb_text: str, ses_path: Path, kicad_cli: object) -> str:
        calls.append(ses_path)
        return pcb_text.rstrip(")") + ' (segment (net "GND")))'

    monkeypatch.setattr(freerouting, "import_ses_with_kicad", fake_import)

    def writer(mutate):  # noqa: ANN001, ANN202
        board.write_text(mutate(board.read_text(encoding="utf-8")), encoding="utf-8")
        return str(board)

    pcb, route, error = anyio.run(autorouter._apply_ses_headless, ses, writer)
    assert error is None and pcb == board and calls == [ses]
    assert '(segment (net "GND"))' in board.read_text(encoding="utf-8")
