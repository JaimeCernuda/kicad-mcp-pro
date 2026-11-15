"""The MCP Apps views: opt-in registration, view tools, data tools and revisions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer as FastMCP

from kicad_mcp import views
from kicad_mcp.views import data
from kicad_mcp.views.revisions import RevisionTracker

VIEW_TOOLS = {
    "view_board",
    "view_schematic",
    "view_bom",
    "view_checks",
    "view_stackup",
    "view_parts",
}
DATA_TOOLS = {
    "view_data_board",
    "view_data_schematic",
    "view_data_glb",
    "view_data_bom",
    "view_data_checks",
    "view_data_stackup",
    "view_data_parts",
    "view_data_compare",
    "view_wait_revision",
}


def _server(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> FastMCP:
    if enabled:
        monkeypatch.setenv(views.APPS_ENV, "1")
    else:
        monkeypatch.delenv(views.APPS_ENV, raising=False)
    server = FastMCP("x")
    views.register(server)
    return server


def _tools(server: FastMCP) -> dict[str, Any]:
    return {tool.name: tool for tool in server._tool_manager.list_tools()}


def test_without_the_opt_in_no_ui_resources_or_ui_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _server(monkeypatch, enabled=False)
    tools = _tools(server)
    assert VIEW_TOOLS | DATA_TOOLS <= set(tools)
    assert all(tools[name].meta is None for name in VIEW_TOOLS | DATA_TOOLS)
    resources = asyncio.run(server.list_resources())
    assert not [r for r in resources if str(r.uri).startswith("ui://")]


def test_with_the_opt_in_six_views_bound_and_data_tools_app_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _server(monkeypatch, enabled=True)
    tools = _tools(server)
    for name in VIEW_TOOLS:
        view = name.removeprefix("view_")
        assert tools[name].meta == {"ui": {"resourceUri": f"ui://kicad/{view}"}}
    for name in DATA_TOOLS:
        assert tools[name].meta["ui"]["visibility"] == ["app"]
    resources = {str(r.uri): r for r in asyncio.run(server.list_resources())}
    assert sorted(uri for uri in resources if uri.startswith("ui://kicad/")) == sorted(
        f"ui://kicad/{view}" for view in views.VIEWS
    )
    assert all(resources[f"ui://kicad/{v}"].mime_type == views.APP_MIME_TYPE for v in views.VIEWS)


@pytest.mark.parametrize("view", views.VIEWS)
def test_each_view_page_names_its_view_and_loads_nothing(view: str) -> None:
    page = views.view_html(view)
    assert f'window.KICAD_VIEW = "{view}";' in page
    assert page.index("window.KICAD_VIEW") < page.index('<script type="module">')
    assert "<script src=" not in page and "<link " not in page


def test_revisions_rise_on_every_change_and_wait_returns() -> None:
    state = {"digest": "a"}
    tracker = RevisionTracker(lambda: ("/p", state["digest"]))
    first = tracker.current()
    assert tracker.current() == first
    state["digest"] = "b"
    assert tracker.current() == first + 1
    assert tracker.wait(first + 1, timeout_s=0.0) == (first + 1, False)
    assert tracker.wait(first, timeout_s=0.0) == (first + 1, True)


def test_board_data_comes_from_the_file_without_kicad_and_says_why(
    sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp.config import get_config

    get_config().apply_project(sample_project)
    monkeypatch.setattr(data, "live_board_text", lambda: None)
    board = data.board_data()
    assert board["source"] == "file" and board["board_name"] == "demo.kicad_pcb"
    assert board["note"].startswith("Edits go to the board file")
    monkeypatch.setattr(data, "live_board_text", lambda: "(kicad_pcb (footprint x))")
    assert data.board_data()["source"] == "live"


def test_schematic_data_lists_every_sheet(sample_project: Path) -> None:
    from kicad_mcp.config import get_config

    (sample_project / "sub").mkdir()
    (sample_project / "sub" / "power.kicad_sch").write_text("(kicad_sch)\n")
    get_config().apply_project(sample_project)
    names = [row["name"] for row in data.schematic_data()["files"]]
    assert names[0] == "demo.kicad_sch" and "sub/power.kicad_sch" in names


def test_checks_data_groups_positions_from_kicad_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    from kicad_mcp.tools import validation

    drc = {
        "violations": [
            {
                "type": "courtyards_overlap",
                "severity": "error",
                "description": "Courtyards overlap",
                "items": [{"description": "U1", "pos": {"x": 10.0, "y": 5.5}, "uuid": "a"}],
            }
        ],
        "unconnected_items": [],
    }
    erc = {
        "sheets": [
            {
                "path": "/",
                "violations": [
                    {
                        "type": "pin_not_connected",
                        "severity": "warning",
                        "description": "Pin",
                        "items": [],
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(validation, "_run_drc_report", lambda name: (Path(name), drc, None))
    monkeypatch.setattr(validation, "_run_erc_report", lambda name: (Path(name), erc, None))
    result = data.checks_data("both")
    assert [v["rule"] for v in result["violations"]] == ["courtyards_overlap", "pin_not_connected"]
    assert result["violations"][0]["items"][0]["position_mm"] == {"x": 10.0, "y": 5.5}
    assert result["summaries"] == {"drc": "1 violations, 0 unconnected", "erc": "1 violations"}


def test_view_board_answers_in_text_for_a_host_without_apps(
    sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp.config import get_config

    get_config().apply_project(sample_project)
    monkeypatch.setattr(data, "live_board_text", lambda: None)
    server = _server(monkeypatch, enabled=False)
    text = _tools(server)["view_board"].fn()
    assert text.startswith("Opened the board view of project as text only")
    assert "board from the board file" in text


def test_board_summary_counts() -> None:
    text = (
        '(kicad_pcb\n  (net 0 "")\n  (net 1 "GND")\n  (footprint "R")\n  (segment (start 0 0))\n)'
    )
    assert data.board_summary(text) == {"footprints": 1, "nets": 2, "tracks": 1}
