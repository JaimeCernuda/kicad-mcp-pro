"""The KiCad instance the server attaches to or starts (KICAD_MCP_KICAD_INSTANCE)."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess  # nosec B404 - test processes
from pathlib import Path

import pytest

from kicad_mcp.instance import config_home
from kicad_mcp.instance import manager as instance_manager
from kicad_mcp.instance.manager import InstanceManager, clear_stale_locks


def test_seeded_settings_skip_the_first_run_wizard_and_enable_the_api(tmp_path: Path) -> None:
    template = tmp_path / "share" / "kicad" / "template"
    template.mkdir(parents=True)
    (template / "sym-lib-table").write_text("(sym_lib_table (version 7) (lib (name X)))\n")
    (template / "fp-lib-table").write_text("(fp_lib_table (version 7))\n")
    cli = tmp_path / "bin" / "kicad-cli"
    cli.parent.mkdir()
    cli.write_text("")
    folder = config_home.seed(tmp_path / "cfg", "10.0.8", cli)
    assert folder.name == "10.0"
    common = json.loads((folder / "kicad_common.json").read_text())
    assert common["api"]["enable_server"] is True
    assert common["do_not_show_again"]["data_collection_prompt"] is True
    assert "(name X)" in (folder / "sym-lib-table").read_text()
    # KiCad 10 ships no design-block template; an empty table is what skips the wizard.
    assert "(design_block_lib_table" in (folder / "design-block-lib-table").read_text()
    assert config_home.api_enabled(tmp_path / "cfg", "10.0.8")


def test_tables_seeded_before_kicad_was_installed_are_filled_in_once_it_is(
    tmp_path: Path,
) -> None:
    folder = config_home.seed(tmp_path / "cfg", None, None)
    assert "(lib " not in (folder / "sym-lib-table").read_text()
    template = tmp_path / "share" / "kicad" / "template"
    template.mkdir(parents=True)
    (template / "sym-lib-table").write_text("(sym_lib_table (version 7) (lib (name X)))\n")
    cli = tmp_path / "bin" / "kicad-cli"
    cli.parent.mkdir()
    cli.write_text("")
    config_home.seed(tmp_path / "cfg", "10.0.8", cli)
    assert "(name X)" in (folder / "sym-lib-table").read_text()


def test_seeding_keeps_existing_settings_but_turns_the_api_back_on(tmp_path: Path) -> None:
    folder = tmp_path / "cfg" / "10.99"
    folder.mkdir(parents=True)
    (folder / "kicad_common.json").write_text(
        json.dumps({"api": {"enable_server": False}, "system": {"language": "German"}})
    )
    config_home.seed(tmp_path / "cfg", "10.99.0-nightly", None)
    common = json.loads((folder / "kicad_common.json").read_text())
    assert common["api"]["enable_server"] is True
    assert common["system"]["language"] == "German"


@pytest.mark.parametrize(("text", "folder"), [("10.0.8", "10.0"), ("11.0.1", "11.0"), ("", "10.0")])
def test_settings_folder_follows_the_version(text: str, folder: str) -> None:
    assert config_home.settings_version(text) == folder


def test_stale_locks_from_this_host_are_removed_only_when_no_kicad_has_the_board(
    tmp_path: Path,
) -> None:
    board = tmp_path / "led.kicad_pcb"
    board.write_text("(kicad_pcb)")
    lock = tmp_path / "~led.kicad_pcb.lck"
    other = tmp_path / "~led.kicad_pro.lck"
    foreign = tmp_path / "~led.kicad_sch.lck"
    host = socket.gethostname()
    lock.write_text(json.dumps({"hostname": host, "username": "me"}))
    other.write_text(json.dumps({"hostname": host, "username": "me"}))
    foreign.write_text(json.dumps({"hostname": "someone-elses-machine", "username": "x"}))
    assert clear_stale_locks(board, lambda _board: True) == []
    assert lock.exists()
    removed = clear_stale_locks(board, lambda _board: False)
    assert sorted(removed) == ["~led.kicad_pcb.lck", "~led.kicad_pro.lck"]
    assert foreign.exists()


def test_attach_mode_never_starts_kicad_and_says_how(monkeypatch, tmp_path: Path) -> None:
    board = tmp_path / "led.kicad_pcb"
    board.write_text("(kicad_pcb)")
    monkeypatch.delenv(instance_manager.INSTANCE_ENV, raising=False)
    monkeypatch.setattr(instance_manager, "probe", lambda url: instance_manager.Probe(False))
    started: list[object] = []
    monkeypatch.setattr(InstanceManager, "_start", lambda *args: started.append(args))
    status = InstanceManager().ensure(board)
    assert status.case == "none" and not started
    assert "KICAD_MCP_KICAD_INSTANCE=auto" in status.next_step


def test_a_running_kicad_with_the_board_is_attached_not_started(
    monkeypatch, tmp_path: Path
) -> None:
    board = tmp_path / "led.kicad_pcb"
    board.write_text("(kicad_pcb)")
    monkeypatch.setenv(instance_manager.INSTANCE_ENV, "auto")
    monkeypatch.setattr(
        instance_manager,
        "probe",
        lambda url: instance_manager.Probe(True, True, "10.0.8", (str(board),)),
    )
    status = InstanceManager().ensure(board)
    assert status.case == "attached"
    assert status.started_by_server is False


def test_no_kicad_on_the_machine_says_where_it_looked(monkeypatch, tmp_path: Path) -> None:
    board = tmp_path / "led.kicad_pcb"
    board.write_text("(kicad_pcb)")
    monkeypatch.setenv(instance_manager.INSTANCE_ENV, "auto")
    monkeypatch.setattr(instance_manager, "probe", lambda url: instance_manager.Probe(False))
    monkeypatch.setattr(instance_manager, "_cli_candidates", lambda configured: [])
    monkeypatch.setattr(instance_manager, "_pcbnew_for", lambda cli: None)
    status = InstanceManager().ensure(board)
    assert status.problem == (
        "KiCad was not found in KICAD_CLI_PATH, PATH or the standard install folders."
    )
    assert status.next_step == (
        "If it is installed elsewhere, set kicad_cli; otherwise see install-kicad."
    )


def test_no_kicad_is_the_only_cause_a_tool_shows(monkeypatch) -> None:
    """The IPC miss that led to looking for KiCad is not why the tool failed."""
    from kicad_mcp import connection, instance, server
    from kicad_mcp.connection import KiCadConnectionError

    def refused() -> object:
        raise KiCadConnectionError("The KiCad IPC connection dropped") from ConnectionError(
            "Connection refused"
        )

    missing = instance_manager.InstanceStatus(
        problem=instance_manager._NOT_FOUND, next_step=instance_manager._NOT_FOUND_NEXT
    )
    monkeypatch.setenv(instance_manager.INSTANCE_ENV, "auto")
    monkeypatch.setattr(connection, "_get_board", refused)
    monkeypatch.setattr(
        instance, "get_instance_manager", lambda: type("M", (), {"ensure": lambda *a: missing})()
    )
    with pytest.raises(KiCadConnectionError) as caught:
        connection.get_board()
    text = server._result_text(server._structured_tool_error(caught.value))
    assert instance_manager._NOT_FOUND in text
    assert "Cause:" not in text
    assert "IPC" not in text


def test_no_display_and_no_xvfb_says_what_to_install(monkeypatch, tmp_path: Path) -> None:
    board = tmp_path / "led.kicad_pcb"
    board.write_text("(kicad_pcb)")
    cli = tmp_path / "kicad-cli"
    cli.write_text("")
    monkeypatch.setenv(instance_manager.INSTANCE_ENV, "auto")
    monkeypatch.setenv(instance_manager.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.setattr(instance_manager, "probe", lambda url: instance_manager.Probe(False))
    monkeypatch.setattr(instance_manager, "_cli_candidates", lambda configured: [cli])
    monkeypatch.setattr(instance_manager, "_supports_api_server", lambda path: False)
    monkeypatch.setattr(instance_manager, "_pcbnew_for", lambda path: tmp_path / "pcbnew")
    monkeypatch.setattr(instance_manager, "_has_display", lambda: False)
    monkeypatch.setattr(instance_manager.shutil, "which", lambda name: None)
    status = InstanceManager().ensure(board)
    assert "xvfb-run was not found" in status.problem
    assert "api-server" in status.next_step


def test_no_board_asks_for_a_project(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(instance_manager, "_cli_candidates", lambda configured: [tmp_path])
    status = InstanceManager().ensure(None)
    assert status.problem.startswith("No board file")
    assert "kicad_set_project" in status.next_step


def test_no_board_and_no_kicad_says_kicad_is_missing_first(monkeypatch) -> None:
    monkeypatch.setattr(instance_manager, "_cli_candidates", lambda configured: [])
    monkeypatch.setattr(instance_manager, "_pcbnew_for", lambda cli: None)
    status = InstanceManager().ensure(None)
    assert status.problem.startswith("KiCad was not found")
    assert "install-kicad" in status.next_step


def test_a_kicad_installed_after_the_server_started_is_found(monkeypatch, tmp_path: Path) -> None:
    from kicad_mcp import config as config_module
    from kicad_mcp import discovery

    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.delenv("KICAD_MCP_KICAD_CLI", raising=False)
    missing = tmp_path / "none" / "kicad-cli"
    monkeypatch.setattr(config_module, "_discover_kicad_cli", lambda: missing)
    monkeypatch.setattr(config_module, "_discover_library_paths", lambda cli: {})
    seeded: list[Path] = []
    monkeypatch.setattr(
        InstanceManager, "isolate_settings", lambda self, cli: seeded.append(cli) or ""
    )
    config_module.reset_config()
    try:
        assert config_module.get_config().kicad_cli == missing
        installed = tmp_path / "KiCad" / "bin" / "kicad-cli"
        installed.parent.mkdir(parents=True)
        installed.write_text("")
        monkeypatch.setattr(discovery, "find_installed_kicad_cli", lambda: installed)
        monkeypatch.setattr(config_module, "_cli_checked_at", 0.0)
        assert config_module.get_config().kicad_cli == installed
        assert seeded == [installed]
    finally:
        config_module.reset_config()


def test_a_kicad_cli_the_person_named_is_kept_even_when_missing(
    monkeypatch, tmp_path: Path
) -> None:
    from kicad_mcp import discovery
    from kicad_mcp.config import KiCadMCPConfig

    named = tmp_path / "named" / "kicad-cli"
    monkeypatch.setattr(discovery, "find_installed_kicad_cli", lambda: tmp_path)
    with pytest.warns(UserWarning):
        cfg = KiCadMCPConfig(kicad_cli=named)
    assert cfg.refresh_kicad_cli() is False
    assert cfg.kicad_cli == named


def test_status_tool_reports_the_case_and_what_to_do() -> None:
    from mcp.server.mcpserver import MCPServer as FastMCP

    from kicad_mcp.tools import kicad_instance

    server = FastMCP("x")
    kicad_instance.register(server)
    tool = server._tool_manager.get_tool("kicad_session_status")
    text = tool.fn()
    assert text.startswith("KiCad instance:")
    assert '"case":' in text


def test_a_started_window_gets_a_private_temp_folder_and_socket(tmp_path: Path) -> None:
    """KiCad's window puts its socket in its temp folder; ours gets a folder of its own, so no
    other KiCad sharing a temp folder can take or delete that socket."""
    import platform

    from kicad_mcp.instance import manager

    home = tmp_path / ("a-very-long-sandboxed-folder-name" * 3) / "instance"
    temp = manager.private_temp(home)
    assert temp == manager.private_temp(home)  # stable for one instance home
    assert temp != manager.private_temp(tmp_path / "other")  # private to it
    if platform.system() != "Windows":
        socket = manager.started_socket(home).removeprefix("ipc://")
        assert socket == str(temp / "kicad" / "api.sock")
        assert len(socket.encode()) <= manager.SOCKET_PATH_MAX  # a Unix socket path limit


def test_the_sweep_never_stops_this_servers_own_kicad(tmp_path: Path) -> None:
    import json
    import os

    from kicad_mcp.instance import manager

    (tmp_path / manager._RECORD).write_text(json.dumps({"pid": 1, "server": os.getpid()}))
    assert manager.stop_left_over(tmp_path) is False


def test_auto_mode_keeps_every_kicad_the_server_runs_off_the_persons_settings(
    monkeypatch, tmp_path: Path
) -> None:
    """KQ8: kicad-cli runs (exports, checks, the 3D model) use the private settings folder."""
    import os

    from kicad_mcp.instance import manager

    monkeypatch.setattr(manager, "_cli_candidates", lambda _configured: [])
    monkeypatch.setenv(manager.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)

    monkeypatch.setenv(manager.INSTANCE_ENV, "attach")
    assert manager.InstanceManager().isolate_settings(None) == ""
    assert "KICAD_CONFIG_HOME" not in os.environ

    monkeypatch.setenv(manager.INSTANCE_ENV, "auto")
    used = manager.InstanceManager().isolate_settings(None)
    assert used == str(tmp_path / "home" / "config")
    assert os.environ["KICAD_CONFIG_HOME"] == used
    assert any((tmp_path / "home" / "config").glob("*/kicad_common.json"))

    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "mine"))
    assert manager.InstanceManager().isolate_settings(None) == ""
    assert os.environ["KICAD_CONFIG_HOME"] == str(tmp_path / "mine")


def test_auto_mode_gives_kicad_a_private_documents_folder(monkeypatch, tmp_path: Path) -> None:
    """KiCad makes Documents/KiCad at every start; a KiCad the server runs uses its own."""
    import os

    from kicad_mcp.instance import manager

    monkeypatch.setattr(manager, "_cli_candidates", lambda _configured: [])
    monkeypatch.setenv(manager.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.setenv(manager.INSTANCE_ENV, "auto")
    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)
    monkeypatch.delenv("KICAD_DOCUMENTS_HOME", raising=False)

    manager.InstanceManager().isolate_settings(None)
    assert os.environ["KICAD_DOCUMENTS_HOME"] == str(tmp_path / "home" / "documents")
    assert (tmp_path / "home" / "documents").is_dir()

    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "mine"))
    assert manager.documents_home(tmp_path / "home") is None
    manager.InstanceManager().isolate_settings(None)
    assert os.environ["KICAD_DOCUMENTS_HOME"] == str(tmp_path / "mine")


def _fake(name: str) -> subprocess.Popen[bytes]:
    bash = shutil.which("bash") or "/bin/bash"
    return subprocess.Popen([bash, "-c", f"exec -a {name} sleep 30"], start_new_session=True)


def _dead_pid() -> int:
    done = subprocess.Popen([shutil.which("true") or "/bin/true"])
    done.wait()
    return done.pid


def test_a_kicad_left_by_a_killed_server_is_stopped_on_the_next_start(tmp_path: Path) -> None:
    import json
    import platform
    import time

    import pytest

    from kicad_mcp.instance import manager

    if platform.system() != "Linux":
        pytest.skip("reads /proc")
    left = _fake("kicad-cli-api-server")
    stranger = _fake("not-a-cad-tool")
    try:
        time.sleep(0.2)
        record = tmp_path / manager._RECORD
        # A live server owns its KiCad: left alone.
        record.write_text(json.dumps({"pid": left.pid, "server": 1}))
        assert manager.stop_left_over(tmp_path) is False and left.poll() is None
        # A reused pid that is not KiCad: never signalled, the record is dropped.
        record.write_text(json.dumps({"pid": stranger.pid, "server": _dead_pid()}))
        assert manager.stop_left_over(tmp_path) is False and stranger.poll() is None
        assert not record.exists()
        # A KiCad whose server is gone: stopped.
        record.write_text(json.dumps({"pid": left.pid, "server": _dead_pid()}))
        assert manager.stop_left_over(tmp_path) is True
        left.wait(timeout=5)
        assert not record.exists()
    finally:
        for proc in (left, stranger):
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_a_development_build_is_used_only_when_named(monkeypatch, tmp_path: Path) -> None:
    from kicad_mcp.instance import manager

    stable, nightly = tmp_path / "kicad-cli", tmp_path / "kicad-cli-nightly"
    for cli in (stable, nightly):
        cli.write_text("")
    versions = {stable: "10.0.8", nightly: "10.99.0"}
    monkeypatch.setattr(manager, "_version", lambda cli: versions.get(cli, "10.99.0"))
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.delenv("KICAD_MCP_KICAD_CLI", raising=False)
    monkeypatch.setattr(manager.shutil, "which", lambda name: str(stable))
    assert manager._cli_candidates(None) == [stable]
    monkeypatch.setattr(manager.shutil, "which", lambda name: str(nightly))
    assert manager._cli_candidates(None) == []  # only a nightly: not picked unasked
    monkeypatch.setenv("KICAD_CLI_PATH", str(nightly))
    assert manager._cli_candidates(nightly) == [nightly]  # named: used


def test_kicad_requests_go_one_at_a_time(monkeypatch) -> None:
    """KiCad's IPC is one request/reply socket; parallel tool calls must not share it at once."""
    import threading
    import time

    from kicad_mcp import connection

    state = {"in_flight": 0, "most": 0}
    guard = threading.Lock()

    class _Client:
        def send(self, command: object, response_type: object) -> object:
            with guard:
                state["in_flight"] += 1
                state["most"] = max(state["most"], state["in_flight"])
            time.sleep(0.01)
            with guard:
                state["in_flight"] -= 1
            return command

    class _KiCad:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._client = _Client()

    monkeypatch.setattr(connection, "KiCad", _KiCad)
    kicad = connection._OneRequestAtATime()
    threads = [threading.Thread(target=kicad._client.send, args=(n, None)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert state["most"] == 1


def test_the_serialized_client_still_receives_the_socket_path() -> None:
    """The session passes only the arguments the factory declares; losing ``socket_path``
    connected every tool to KiCad's default socket instead of the started KiCad's."""
    from kicad_mcp import connection
    from kicad_mcp.kicad.session import KiCadSession

    session = KiCadSession(client_factory=connection._OneRequestAtATime)
    assert "socket_path" in session._constructor_params()


def test_a_private_temp_someone_else_planted_is_refused(tmp_path: Path) -> None:
    from kicad_mcp.instance import manager

    real = tmp_path / "elsewhere"
    real.mkdir()
    planted = tmp_path / "kmcp-planted"
    planted.symlink_to(real)
    assert manager._own_private_temp(planted) is False
    mine = tmp_path / "kmcp-mine"
    assert manager._own_private_temp(mine) is True
    assert (mine.stat().st_mode & 0o777) == 0o700


def test_status_names_the_persons_running_kicad_before_any_project(
    monkeypatch, tmp_path: Path
) -> None:
    """Found live (KQ6): with the person's KiCad open and no project set yet, the status
    said "no KiCad instance yet" while IPC was already talking to that KiCad."""
    monkeypatch.setattr(
        instance_manager,
        "probe",
        lambda url: instance_manager.Probe(True, True, "10.0.8", ("led.kicad_pcb",)),
    )
    status = InstanceManager().status()
    assert status.case == "attached"
    assert status.version == "10.0.8"
    assert status.board == "led.kicad_pcb"
    assert status.started_by_server is False


def test_status_with_no_kicad_running_still_says_none(monkeypatch) -> None:
    monkeypatch.setattr(instance_manager, "probe", lambda url: instance_manager.Probe(False))
    assert InstanceManager().status().case == "none"
