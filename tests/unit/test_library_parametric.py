"""Parametric search, spec sheets, part comparison and specs-based derating."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer as FastMCP

from kicad_mcp.library.parametric import (
    JLCSearchCategories,
    ParametricService,
    configured_sources,
    rating_from_specs,
    unconfigured_sources,
)
from kicad_mcp.tools import library_parametric
from kicad_mcp.utils.component_search import ComponentRecord
from kicad_mcp.utils.part_specs import (
    SpecFilter,
    parse_quantity,
    specs_from_description,
    specs_from_digikey,
    specs_from_jlcsearch,
    specs_from_mouser,
    specs_from_nexar,
)

CAP_ROWS = [
    {
        "lcsc": 237493,
        "mfr": "GRM21BZ71E106KE15L",
        "description": "10uF 25V X7R ±10% 0805 MLCC",
        "stock": 75633,
        "price1": 0.2639,
        "capacitance_farads": 1e-5,
        "tolerance_fraction": 0.1,
        "voltage_rating": 25,
        "package": "0805",
        "temperature_coefficient": "X7R",
        "attributes": json.dumps({"Capacitance": "10uF", "Voltage Rating": "25V"}),
    },
    {
        "lcsc": 15850,
        "mfr": "CL21A106KAYNNNE",
        "description": "10uF 25V X5R ±10% 0805 MLCC",
        "stock": 900000,
        "price1": 0.01,
        "capacitance_farads": 1e-5,
        "voltage_rating": 25,
        "package": "0805",
        "temperature_coefficient": "X5R",
    },
    {
        "lcsc": 1,
        "mfr": "LOWVOLT",
        "description": "10uF 16V X7R 0805",
        "stock": 10,
        "price1": 0.02,
        "capacitance_farads": 1e-5,
        "voltage_rating": 16,
        "package": "0805",
        "temperature_coefficient": "X7R",
    },
]


class _Client:
    def __init__(self, records: list[ComponentRecord], *, fail: Exception | None = None) -> None:
        self.records = records
        self.fail = fail

    def search(self, keyword: str, **_kwargs: object) -> list[ComponentRecord]:
        if self.fail:
            raise self.fail
        return self.records

    def get_part(self, code: str) -> ComponentRecord | None:
        if self.fail:
            raise self.fail
        return next((r for r in self.records if code in (r.mpn, r.lcsc_code)), None)


def _service(
    *, env: dict[str, str] | None = None, jlc_fail: Exception | None = None, **clients: _Client
):
    calls: list[tuple[str, dict[str, object]]] = []

    def request_json(url: str, params: dict[str, object]) -> dict[str, Any]:
        calls.append((url, params))
        if jlc_fail:
            raise jlc_fail
        return {"capacitors": CAP_ROWS}

    def client_for(source: str) -> _Client:
        return clients[source]

    service = ParametricService(
        client_for, categories=JLCSearchCategories(request_json), env=env or {}
    )
    return service, calls


@pytest.mark.parametrize(
    ("text", "number", "unit"),
    [
        ("10µF", 1e-5, "F"),
        ("0.01 mF", 1e-5, "F"),
        ("100nF", 1e-7, "F"),
        ("0.1uF", 1e-7, "F"),
        ("25V", 25.0, "V"),
        ("±10%", 10.0, "%"),
        ("4.7k", 4700.0, ""),
        ("10kΩ", 1e4, "Ω"),
        ("500mA", 0.5, "A"),
        ("-40℃", -40.0, "°C"),
    ],
)
def test_units_are_normalized(text: str, number: float, unit: str) -> None:
    found, found_unit = parse_quantity(text)
    assert found == pytest.approx(number)
    assert found_unit == unit


def test_not_a_quantity() -> None:
    assert parse_quantity("X7R") == (None, "")
    assert parse_quantity("10pcs") == (None, "")


def test_100nf_equals_0_1uf() -> None:
    spec = specs_from_description("100nF 50V X7R", "x")
    assert SpecFilter.from_mapping({"name": "capacitance", "value": "0.1", "unit": "uF"}).matches(
        spec
    )


def test_every_source_shape_becomes_specs() -> None:
    nexar = specs_from_nexar([{"attribute": {"name": "Capacitance"}, "displayValue": "10 µF"}])
    digikey = specs_from_digikey([{"ParameterText": "Voltage - Rated", "ValueText": "25V"}])
    mouser = specs_from_mouser([{"AttributeName": "Dielectric", "AttributeValue": "X7R"}])
    jlc = specs_from_jlcsearch(CAP_ROWS[0])
    assert (nexar[0].name, nexar[0].number, nexar[0].source) == (
        "capacitance",
        pytest.approx(1e-5),
        "nexar",
    )
    assert (digikey[0].name, digikey[0].number) == ("voltage_rating", 25.0)
    assert (mouser[0].name, mouser[0].display) == ("temperature_coefficient", "X7R")
    by_name = {s.name: s for s in jlc}
    assert by_name["capacitance"].number == pytest.approx(1e-5)
    assert by_name["tolerance"].number == pytest.approx(10.0)
    assert by_name["voltage_rating"].label == "voltage_rating"


def test_filters_validate() -> None:
    with pytest.raises(ValueError, match="op must be"):
        SpecFilter.from_mapping({"name": "capacitance", "op": "~", "value": "1"})
    with pytest.raises(ValueError, match="value is empty"):
        SpecFilter.from_mapping({"name": "capacitance", "value": ""})


def test_search_applies_every_filter_and_server_side_capacitance() -> None:
    service, calls = _service()
    answer = service.search(
        "capacitor",
        [
            {"name": "capacitance", "op": "=", "value": "10", "unit": "uF"},
            {"name": "voltage_rating", "op": ">=", "value": "25", "unit": "V"},
            {"name": "temperature_coefficient", "op": "=", "value": "X7R"},
        ],
        package="0805",
        min_stock=1,
    )
    assert [r.mpn for r in answer.records] == ["GRM21BZ71E106KE15L"]
    url, params = calls[0]
    assert url.endswith("/capacitors/list.json")
    assert params["capacitance"] == "1e-05"
    assert params["package"] == "0805"
    assert answer.account() == {"answered_by": ["jlcsearch"], "failed": [], "could_configure": []}


def test_a_failing_primary_source_says_what_could_be_configured() -> None:
    service, _ = _service(jlc_fail=OSError("HTTP Error 429: Too Many Requests"))
    answer = service.search("capacitors", [])
    account = answer.account()
    assert account["answered_by"] == []
    assert account["failed"][0]["source"] == "jlcsearch"
    assert [row["source"] for row in account["could_configure"]] == ["nexar", "digikey", "mouser"]
    assert account["could_configure"][0]["variables"] == ["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"]
    text = library_parametric.format_search("capacitors", answer)
    assert "nexar is not configured" in text and "NEXAR_CLIENT_ID" in text


def test_configured_sources_follow_the_environment() -> None:
    env = {"NEXAR_CLIENT_ID": "id", "NEXAR_CLIENT_SECRET": "secret", "MOUSER_API_KEY": ""}
    assert configured_sources(env) == ["jlcsearch", "nexar"]
    assert [row["source"] for row in unconfigured_sources(env)] == ["digikey", "mouser"]


def test_spec_sheet_merges_sources_and_names_each() -> None:
    nexar_record = ComponentRecord(
        source="nexar",
        lcsc_code="",
        mpn="GRM21BZ71E106KE15L",
        package="0805",
        description="",
        stock=5,
        price=None,
        is_basic=False,
        is_preferred=False,
        specs=specs_from_nexar(
            [{"attribute": {"name": "Thermal Resistance"}, "displayValue": "40 K/W"}]
        ),
    )
    jlc_record = ComponentRecord(
        source="jlcsearch",
        lcsc_code="C237493",
        mpn="GRM21BZ71E106KE15L",
        package="0805",
        description="10uF 25V X7R ±10% 0805 MLCC",
        stock=75633,
        price=0.26,
        is_basic=False,
        is_preferred=False,
    )
    env = {"NEXAR_CLIENT_ID": "i", "NEXAR_CLIENT_SECRET": "s"}
    service, _ = _service(env=env, jlcsearch=_Client([jlc_record]), nexar=_Client([nexar_record]))
    _records, specs, answer = service.spec_sheet("GRM21BZ71E106KE15L", category="capacitors")
    sources = {s.source for s in specs}
    assert {"jlcsearch", "nexar"} <= sources
    assert any(s.name == "thermal_resistance" and s.source == "nexar" for s in specs)
    assert answer.answered_by == ["jlcsearch", "nexar"]


def test_compare_puts_parts_side_by_side() -> None:
    records = [
        ComponentRecord("jlcsearch", "C1", "A", "0805", "10uF 25V X7R", 1, 0.1, False, False),
        ComponentRecord("jlcsearch", "C2", "B", "0805", "10uF 50V X5R", 1, 0.1, False, False),
    ]
    service, _ = _service(jlcsearch=_Client(records))
    parts, rows, _answer = service.compare(["A", "B"], ["voltage_rating"])
    assert parts == ["A", "B"]
    assert rows[0]["A"][0]["display"] == "25V"
    assert rows[0]["B"][0]["display"] == "50V"


def test_derating_rating_comes_from_specs() -> None:
    record = ComponentRecord("jlcsearch", "C1", "A", "0805", "10uF 25V X7R", 1, 0.1, False, False)
    service, _ = _service(jlcsearch=_Client([record]))
    found = rating_from_specs("A", "voltage", service)
    assert not isinstance(found, str)
    assert found.number == 25.0
    missing = rating_from_specs("A", "current", service)
    assert isinstance(missing, str) and "rated_value" in missing


def test_tools_register_with_their_descriptions() -> None:
    server = FastMCP("x")
    service, _ = _service()
    library_parametric.register(server, library_parametric.LibraryParametricDependencies(service))
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert {"lib_search_parametric", "lib_get_component_specs", "lib_compare_parts"} <= names
    tool = server._tool_manager.get_tool("lib_search_parametric")
    text = tool.fn(
        category="capacitors", filters=[{"name": "voltage_rating", "op": ">=", "value": "25V"}]
    )
    assert "GRM21BZ71E106KE15L" in text and "Answered by: jlcsearch." in text
    assert "failed" in server._tool_manager.get_tool("lib_compare_parts").fn(parts=[])


def test_failed_sourcing_results_name_the_sources_that_could_be_set_up(monkeypatch) -> None:
    from kicad_mcp.library.parametric import sourcing_hint

    for name in ("NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET", "DIGIKEY_CLIENT_ID", "MOUSER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    hint = sourcing_hint()
    assert "nexar (NEXAR_CLIENT_ID, NEXAR_CLIENT_SECRET)" in hint
    assert (
        sourcing_hint(
            None,
            {
                "NEXAR_CLIENT_ID": "a",
                "NEXAR_CLIENT_SECRET": "b",
                "DIGIKEY_CLIENT_ID": "c",
                "DIGIKEY_CLIENT_SECRET": "d",
                "MOUSER_API_KEY": "e",
            },
        )
        == ""
    )


def test_a_description_names_volts_and_amps_as_ratings_only_for_passives() -> None:
    cap = {spec.name: spec.display for spec in specs_from_description("10uF 25V X7R", "x")}
    ldo = {spec.name: spec.display for spec in specs_from_description("3.3V 200mA LDO", "x")}
    assert cap["voltage_rating"] == "25V"
    assert ldo == {"voltage": "3.3V", "current": "200mA"}  # an output, not a rating


def test_a_parsed_description_only_fills_what_its_source_does_not_report() -> None:
    from kicad_mcp.utils.part_specs import merge_specs

    reported = specs_from_jlcsearch(CAP_ROWS[0])
    parsed = specs_from_description("10uF 25V X7R 0805", "jlcsearch")
    merged = merge_specs((reported, parsed))
    names = [spec.name for spec in merged]
    assert len(names) == len(set(names))  # no name twice from jlcsearch
    assert all(
        not spec.source.endswith(":description") for spec in merged if spec.name == "capacitance"
    )


def test_a_filter_name_no_part_has_is_named_with_the_names_they_do_have() -> None:
    service, _ = _service()
    answer = service.search("capacitors", [{"name": "voltage", "op": ">=", "value": "25V"}])
    assert answer.records == []
    assert "voltage" in answer.unmatched and "voltage_rating" in answer.unmatched["voltage"]
    text = library_parametric.format_search("capacitors", answer)
    assert "No part returned has a spec named 'voltage'" in text and "voltage_rating" in text
