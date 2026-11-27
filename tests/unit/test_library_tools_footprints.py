"""KiCad Library Tools as a child process: generate, file, check (footprints plan FP1-FP6).

A stand-in checkout replaces Library Tools: its ``generate.py`` writes one footprint and one
STEP body for the selected entry file, and its ``check_footprint.py`` prints the KLC
checker's text and exit codes. Nothing here needs the network or the real repository.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from kicad_mcp.library_tools import install, runner

GENERATE = textwrap.dedent(
    """
    import argparse, pathlib, sys
    p = argparse.ArgumentParser()
    p.add_argument("-f"); p.add_argument("-m"); p.add_argument("-g"); p.add_argument("-c")
    p.add_argument("-j")
    a = p.parse_args()
    data = pathlib.Path(__file__).resolve().parents[2] / "data" / a.g
    entry = next(data.glob(a.c.split("/")[-1]))
    if "broken" in entry.read_text():
        print("KeyError: 'pitch'"); sys.exit(2)
    pretty = pathlib.Path(a.f) / "Package_DFN_QFN.pretty"; pretty.mkdir(parents=True)
    (pretty / "Part_QFN-4.kicad_mod").write_text(
        '(footprint "Part_QFN-4"\\n'
        '\\t(pad "1" smd roundrect\\n\\t\\t(at -1 -0.5)\\n\\t\\t(size 0.8 0.3)\\n\\t)\\n'
        '\\t(pad "2" smd roundrect\\n\\t\\t(at -1 0.5)\\n\\t\\t(size 0.8 0.3)\\n\\t)\\n'
        '\\t(pad "3" smd roundrect\\n\\t\\t(at 1 0.5 180)\\n\\t\\t(size 0.8 0.3)\\n\\t)\\n'
        '\\t(pad "4" smd roundrect\\n\\t\\t(at 1 -0.5 180)\\n\\t\\t(size 0.8 0.3)\\n\\t)\\n'
        '\\t(pad "" smd rect\\n\\t\\t(at 0 0)\\n\\t\\t(size 0.4 0.4)\\n\\t)\\n'
        '\\t(model "${KICAD10_3DMODEL_DIR}/Package_DFN_QFN.3dshapes/Part_QFN-4.step"\\n\\t)\\n)\\n'
    )
    if a.m:
        shapes = pathlib.Path(a.m) / "Package_DFN_QFN.3dshapes"; shapes.mkdir(parents=True)
        (shapes / "Part_QFN-4.step").write_text("ISO-10303-21;")
    print("Generation successful!")
    """
)

CHECK = textwrap.dedent(
    """
    import sys
    errors_only = "-w" in sys.argv
    print("Checking footprint 'Part_QFN-4':")
    print("  Violating F5.1 - https://klc.kicad.org/footprint/f5/f5.1/")
    print("    Courtyard missing")
    if not errors_only:
        print("  Violating F6.3 - https://klc.kicad.org/footprint/f6/f6.3/")
        print("  Violating F6.3 - https://klc.kicad.org/footprint/f6/f6.3/")
        print("    Pad requirements for SMD footprints - F6.3 - https://klc.kicad.org/")
        print("    Pad(s) potentially missing layers")
        print("  Violating F9.3 - https://klc.kicad.org/footprint/f9/f9.3/")
        print("    Model path should start with '${KICAD10_3DMODEL_DIR}/'")
    sys.exit(3)
    """
)


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> install.Installed:
    root = tmp_path / "klt"
    generators = root / "src" / "generators"
    (generators / "package" / "no_lead").mkdir(parents=True)
    (generators / "package" / "no_lead" / "spec.py").write_text("")
    (generators / "package" / "no_lead" / "model.py").write_text("")
    (generators / "connector" / "JST").mkdir(parents=True)
    (generators / "connector" / "JST" / "footprint.py").write_text("")
    (generators / "generate.py").write_text(GENERATE)
    data = root / "data" / "package" / "no_lead"
    data.mkdir(parents=True)
    (data / "qfn.yaml").write_text("default_parameters:\n  library: Package_DFN_QFN\n")
    klc = root / "library_utils" / "klc-check"
    klc.mkdir(parents=True)
    (klc / "check_footprint.py").write_text(CHECK)
    monkeypatch.setenv(install.CHECKOUT_ENV, str(root))
    monkeypatch.setenv(install.PYTHON_ENV, sys.executable)
    return install.ensure()


def test_families_are_found_as_generate_py_finds_them(tools: install.Installed) -> None:
    found = {f.name: f for f in runner.families(tools)}
    assert found["package/no_lead"].models and found["package/no_lead"].data == ("qfn.yaml",)
    assert not found["connector/JST"].data
    assert runner.family_example(tools, "package/no_lead").startswith("# data/package/no_lead")
    with pytest.raises(runner.LibraryToolsError, match="reads no YAML entries"):
        runner.generate(tools, "connector/JST", "X:\n  pitch: 1\n", models=False)


def test_a_generated_footprint_is_filed_into_the_project(
    tools: install.Installed, tmp_path: Path
) -> None:
    generated = runner.generate(tools, "package/no_lead", "Part:\n  pitch: 1\n", models=True)
    assert not list((tools.root / "data" / "package" / "no_lead").glob("zz_*"))
    project = tmp_path / "board"
    footprints, models, notes = runner.file_into_project(generated, project, "board")
    runner.file_into_project(generated, project, "board")

    text = footprints[0].read_text()
    assert '(model "${KIPRJMOD}/libs/3d/Part_QFN-4.step"' in text
    assert models == [project / "libs" / "3d" / "Part_QFN-4.step"] and not notes
    table = (project / "fp-lib-table").read_text()
    assert table.count('(name "board")') == 1
    assert "${KIPRJMOD}/libs/board.pretty" in table

    no_body = runner.generate(tools, "package/no_lead", "Part:\n  pitch: 1\n", models=False)
    _, _, notes = runner.file_into_project(no_body, tmp_path / "other", "other")
    assert notes == ["Part_QFN-4: no 3D body was generated for Part_QFN-4.step."]


def test_a_generator_failure_is_reported_with_its_output(tools: install.Installed) -> None:
    with pytest.raises(runner.LibraryToolsError, match="KeyError: 'pitch'"):
        runner.generate(tools, "package/no_lead", "Part:\n  broken: 1\n", models=False)


def test_the_check_names_a_pad_off_the_datasheet_and_sorts_klc_findings(
    tools: install.Installed, tmp_path: Path
) -> None:
    generated = runner.generate(tools, "package/no_lead", "Part:\n  pitch: 1\n", models=False)
    footprint = runner.file_into_project(generated, tmp_path, "board")[0][0]
    pads = runner.parse_pads(footprint.read_text())
    assert [p.number for p in pads] == ["1", "2", "3", "4"]  # the paste pad has no number

    sheet = [runner.Pad(p.number, p.x, p.y, p.w, p.h) for p in pads]
    sheet[2] = runner.Pad("3", 1.3, 0.5, 0.8, 0.3)  # 0.3 mm off
    result = runner.check(tools, footprint, sheet, 0.05)
    assert result.mismatches == ["pad 3: at (1, 0.5) mm, the datasheet (1.3, 0.5) mm"]
    assert result.klc_errors == ["F5.1: Courtyard missing"]
    assert result.klc_warnings == ["F6.3: Pad(s) potentially missing layers"]
    assert result.notes == ["F9.3: Model path should start with '${KICAD10_3DMODEL_DIR}/'"]
    assert not result.passed

    close = [runner.Pad(p.number, p.x + 0.04, p.y, p.w, p.h) for p in pads[:3]]
    assert runner.compare_pads(close, pads, 0.05) == [
        "pads 4: in the footprint, not in the datasheet"
    ]


def test_records_keep_where_a_footprint_came_from_and_its_check(tmp_path: Path) -> None:
    runner.update_record(tmp_path, "Part", rung="family package/no_lead", datasheet="p. 14")
    result = runner.CheckResult(footprint="Part", mismatches=["pad 3: off"], compared=4)
    runner.update_record(tmp_path, "Part", check=runner.check_record(result, "p. 14", 0.05))
    record = json.loads((tmp_path / "libs" / runner.RECORDS).read_text())["Part"]
    assert record["rung"] == "family package/no_lead"
    assert record["check"]["passed"] is False and record["check"]["compared_pads"] == 4


def test_install_says_what_the_person_does_when_it_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(install.CHECKOUT_ENV, str(tmp_path))
    with pytest.raises(install.LibraryToolsInstallError, match="not a Library Tools checkout"):
        install.ensure()

    monkeypatch.delenv(install.CHECKOUT_ENV)
    monkeypatch.setenv("KICAD_MCP_TOOLS_HOME", str(tmp_path / "tools"))
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    with pytest.raises(install.LibraryToolsInstallError) as raised:
        install.ensure()
    assert "needs git" in str(raised.value)
    assert install.CHECKOUT_ENV in str(raised.value) and install.pin() in str(raised.value)


def test_the_four_tools_are_registered() -> None:
    import asyncio

    from fastmcp import FastMCP

    from kicad_mcp.tools import library_footprints_klt

    mcp = FastMCP("t")
    library_footprints_klt.register(mcp)
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names == {
        "lib_list_footprint_families",
        "lib_generate_footprint",
        "lib_generate_footprint_custom",
        "lib_check_footprint",
    }


def test_two_mounting_tabs_with_one_number_are_compared_with_two_tabs() -> None:
    """Found live (FQ3): a JST SMD footprint's two tabs are both pad "MP"; the check compared
    both datasheet tabs with one footprint tab and reported a tab that was in place."""
    footprint = [
        runner.Pad("1", -0.5, 0.0, 0.6, 1.55),
        runner.Pad("2", 0.5, 0.0, 0.6, 1.55),
        runner.Pad("MP", -2.1, -2.0, 1.0, 2.8),
        runner.Pad("MP", 2.1, -2.0, 1.0, 2.8),
    ]
    sheet = [runner.Pad(p.number, p.x, p.y, p.w, p.h) for p in footprint]
    assert runner.compare_pads(sheet, footprint, 0.05) == []
    moved = [*sheet[:3], runner.Pad("MP", 2.4, -2.0, 1.0, 2.8)]
    assert runner.compare_pads(moved, footprint, 0.05) == [
        "pad MP: at (2.1, -2) mm, the datasheet (2.4, -2) mm"
    ]


def test_an_exposed_pad_is_compared_with_itself_not_its_vias() -> None:
    footprint = [
        runner.Pad("9", 0.0, 0.0, 1.6, 1.6),
        runner.Pad("9", -0.5, -0.5, 0.3, 0.3),
        runner.Pad("9", 0.5, 0.5, 0.3, 0.3),
    ]
    assert runner.compare_pads([runner.Pad("9", 0.0, 0.0, 1.6, 1.6)], footprint, 0.05) == []


def test_assigning_a_recorded_footprint_records_its_acceptance(tmp_path: Path) -> None:
    """Found live (FQ4): the record kept accepted false after the person accepted the
    module's custom footprint and it was assigned."""
    runner.update_record(tmp_path, "E22-900M22S", rung="custom", accepted=False)
    assert runner.mark_assigned(tmp_path, "E22-900M22S", "U4")
    record = json.loads((tmp_path / "libs" / runner.RECORDS).read_text())["E22-900M22S"]
    assert record["accepted"] is True and record["assigned_to"] == ["U4"]
    assert not runner.mark_assigned(tmp_path, "SOT-23-5", "U2")  # a library footprint
    assert "SOT-23-5" not in json.loads((tmp_path / "libs" / runner.RECORDS).read_text())


def test_the_family_list_names_each_familys_data_files(tools: install.Installed) -> None:
    """Found live (FQ3): the list said "4 data files", not that JST's SMD series are
    entries in ``conn_jst``, and the agent wrote a one-off script for a JST connector."""
    from kicad_mcp.tools.library_footprints_klt import _data_files

    assert _data_files(("conn_hirose.yaml", "conn_jst.yaml")) == "data files: conn_hirose, conn_jst"
    many = tuple(f"f{i}.yaml" for i in range(10))
    assert _data_files(many).endswith("f7, ... (10 in all)")
    data = tools.root / "data" / "package" / "no_lead"
    (data / "jst.yaml").write_text("Part:\n  pitch: 1\n")
    assert runner.family_example(tools, "package/no_lead", prefer="JST").startswith(
        "# data/package/no_lead/jst.yaml"
    )
    assert runner.family_example(tools, "package/no_lead", prefer="none").startswith(
        "# data/package/no_lead/jst.yaml"  # no match: the first file
    )


def test_the_family_list_names_library_tools_python_for_cadquery_scripts(
    tools: install.Installed, tmp_path: Path
) -> None:
    """Found live (FQ7): each conversation installed CadQuery again (about 1.7 GB in its
    scratch) to run the enclosure scripts, while Library Tools' environment has it."""
    from kicad_mcp.tools.library_footprints_klt import _python_line

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    with_cq = install.Installed(tools.root, venv / "bin" / "python", tools.pin, models=True)
    assert _python_line(with_cq).endswith(
        "with KicadModTree (no CadQuery yet): " + str(venv / "bin" / "python")
    )
    (venv / "lib" / "python3.13" / "site-packages" / "cadquery").mkdir(parents=True)
    assert "with KicadModTree and CadQuery: " in _python_line(with_cq)


def test_a_footprint_file_is_checked_with_no_project_set(
    tools: install.Installed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live (FQ5): the check ran, then failed with "No project is set" while recording
    its result, and the agent compared the pads by hand."""
    import asyncio

    from fastmcp import FastMCP

    from kicad_mcp.config import get_config
    from kicad_mcp.tools import library_footprints_klt

    project = tmp_path / "board"
    library = project / "libs" / "board.pretty"
    library.mkdir(parents=True)
    (project / "board.kicad_pro").write_text("{}")
    footprint = library / "U3.kicad_mod"
    footprint.write_text('(footprint "U3" (pad "1" smd rect (at -1.4125 -0.75) (size 0.625 0.25)))')
    for name in ("project_dir", "project_file", "pcb_file", "sch_file"):
        monkeypatch.setattr(get_config(), name, None)
    monkeypatch.setattr(get_config(), "workspace_root", None)
    monkeypatch.chdir(tmp_path)  # the server runs in its workspace folder
    monkeypatch.setattr(
        runner,
        "check",
        lambda _tools, target, pads, tol: runner.CheckResult(
            footprint=target.stem,
            compared=len(pads),
            mismatches=runner.compare_pads(pads, runner.parse_pads(target.read_text()), tol),
        ),
    )
    mcp = FastMCP("t")
    library_footprints_klt.register(mcp)
    shifted = [{"number": "1", "x": -1.1125, "y": -0.75, "w": 0.625, "h": 0.25}]
    result = asyncio.run(
        mcp.call_tool("lib_check_footprint", {"path": str(footprint), "land_pattern": shifted})
    )
    text = result.content[0].text
    assert "FAIL: U3" in text
    assert "pad 1: at (-1.4125, -0.75) mm, the datasheet (-1.1125, -0.75) mm" in text
    records = json.loads((project / "libs" / "footprint-records.json").read_text())
    assert records["U3"]["check"]["passed"] is False
