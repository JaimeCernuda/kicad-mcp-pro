"""Generate, check and file footprints and 3D models with KiCad Library Tools.

Library Tools runs as a child process (``install.ensure``). A generated footprint lands in the
project library ``<project>/libs/<library>.pretty`` and its body in ``<project>/libs/3d``, with
the model path made relative to the project (``${KIPRJMOD}``) and the library registered in
the project's ``fp-lib-table``, so the board stays portable as a folder. Every footprint made
or checked here has a record in ``<project>/libs/footprint-records.json``: where it came from
(a generator family or a custom script), the datasheet pages it was taken from, and the last
check.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import shutil
import subprocess  # noqa: F401  # nosec B404 - tests replace subprocess.run through this module
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..utils import run_to_end
from .install import Installed

RECORDS = "footprint-records.json"
# KLC rules about a library's naming and its model folder: they bind KiCad's own library,
# not a project library, so they are reported as notes.
NAMING_RULES = frozenset({"G1.1", "F9.1", "F9.3"})


class LibraryToolsError(RuntimeError):
    """A generator or the checker refused the input; the message says why."""


@dataclass(frozen=True)
class Family:
    name: str
    footprints: bool
    models: bool
    data: tuple[str, ...]


@dataclass
class Generated:
    footprints: list[Path] = field(default_factory=list)
    models: list[Path] = field(default_factory=list)
    messages: str = ""


@dataclass(frozen=True)
class Pad:
    number: str
    x: float
    y: float
    w: float
    h: float
    rotation: float = 0.0


@dataclass
class CheckResult:
    footprint: str
    klc_errors: list[str] = field(default_factory=list)
    klc_warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    compared: int = 0

    @property
    def passed(self) -> bool:
        return not self.klc_errors and not self.mismatches


# --- families ---------------------------------------------------------------------------


def families(tools: Installed) -> list[Family]:
    """Every generator family at the pin, as ``generate.py`` finds them."""
    found: list[Family] = []
    base = tools.generators
    for folder in sorted(p for p in base.rglob("*") if p.is_dir()):
        scripts = ("spec.py", "footprint.py", "model.py")
        kinds = {name for name in scripts if (folder / name).exists()}
        if not kinds or "__pycache__" in folder.parts:
            continue
        name = folder.relative_to(base).as_posix()
        data = tools.root / "data" / name
        yamls = tuple(sorted(p.name for p in data.glob("*.yaml"))) if data.is_dir() else ()
        found.append(
            Family(
                name=name,
                footprints="footprint.py" in kinds,
                models="model.py" in kinds,
                data=yamls,
            )
        )
    return found


def family_example(tools: Installed, family: str, max_lines: int = 60) -> str:
    """The head of the family's first data file and its first entry, verbatim."""
    data = tools.root / "data" / family
    files = sorted(data.glob("*.yaml")) if data.is_dir() else []
    if not files:
        return ""
    lines = files[0].read_text(encoding="utf-8", errors="replace").splitlines()
    return f"# {files[0].relative_to(tools.root).as_posix()}\n" + "\n".join(lines[:max_lines])


def family_readers(tools: Installed, family: str) -> list[str]:
    """The family's scripts that read an entry's keys (the schema is the code)."""
    folder = tools.generators / family
    return [
        (folder / name).relative_to(tools.root).as_posix()
        for name in ("spec.py", "footprint.py", "model.py")
        if (folder / name).exists()
    ]


# --- generation -------------------------------------------------------------------------


def generate(tools: Installed, family: str, entry: str, *, models: bool) -> Generated:
    """Run one family's generator on the entry, into a fresh folder."""
    known = {f.name: f for f in families(tools)}
    if family not in known:
        raise LibraryToolsError(f"No generator family is named {family!r}.")
    data = tools.root / "data" / family
    if not known[family].data or not data.is_dir():
        raise LibraryToolsError(
            f"The {family} family reads no YAML entries; write a custom script for this part."
        )
    token = secrets.token_hex(6)
    entry_file = data / f"zz_kicad_mcp_{token}.yaml"
    out = Path(tempfile.mkdtemp(prefix="kicad-mcp-fp-"))
    try:
        entry_file.write_text(entry if entry.endswith("\n") else entry + "\n", encoding="utf-8")
        argv = [
            str(tools.python),
            "generate.py",
            "-f",
            str(out / "fp"),
            "-g",
            family,
            "-c",
            f"*/{entry_file.name}",
            "-j",
            "1",
        ]
        if models:
            argv[4:4] = ["-m", str(out / "3d")]
        code, text = _run(argv, cwd=tools.generators)
        result = _collect(out)
        result.messages = _tail(text)
        if code != 0 or not result.footprints:
            raise LibraryToolsError(
                f"The {family} generator made no footprint (exit {code}): {result.messages}"
            )
        return result
    finally:
        entry_file.unlink(missing_ok=True)


def generate_custom(tools: Installed, script: str) -> Generated:
    """Run a one-off KicadModTree/CadQuery script in Library Tools' environment.

    The script writes its ``.kicad_mod`` files under ``$KICAD_MCP_FOOTPRINT_DIR`` and its
    STEP bodies under ``$KICAD_MCP_MODEL_DIR``.
    """
    out = Path(tempfile.mkdtemp(prefix="kicad-mcp-fp-"))
    (out / "fp").mkdir()
    (out / "3d").mkdir()
    path = out / "part.py"
    path.write_text(script, encoding="utf-8")
    env = {
        "KICAD_MCP_FOOTPRINT_DIR": str(out / "fp"),
        "KICAD_MCP_MODEL_DIR": str(out / "3d"),
        "PYTHONPATH": f"{tools.root / 'src'}:{tools.root}",
    }
    code, text = _run([str(tools.python), str(path)], cwd=out, env=env)
    result = _collect(out)
    result.messages = _tail(text)
    if code != 0 or not result.footprints:
        raise LibraryToolsError(f"The script made no footprint (exit {code}): {result.messages}")
    return result


def _collect(out: Path) -> Generated:
    return Generated(
        footprints=sorted(out.rglob("*.kicad_mod")),
        models=sorted(p for p in out.rglob("*") if p.suffix.lower() in (".step", ".stp")),
    )


def file_into_project(
    generated: Generated, project_dir: Path, library: str
) -> tuple[list[Path], list[Path], list[str]]:
    """Copy footprints and bodies into the project library; model paths become relative."""
    pretty = project_dir / "libs" / f"{library}.pretty"
    shapes = project_dir / "libs" / "3d"
    pretty.mkdir(parents=True, exist_ok=True)
    shapes.mkdir(parents=True, exist_ok=True)
    models: list[Path] = []
    by_name: dict[str, Path] = {}
    for model in generated.models:
        target = shapes / model.name
        shutil.copyfile(model, target)
        models.append(target)
        by_name[model.name] = target
    notes: list[str] = []
    footprints: list[Path] = []
    for footprint in generated.footprints:
        text = footprint.read_text(encoding="utf-8")
        text, missing = _relative_models(text, by_name)
        if missing:
            notes.append(f"{footprint.stem}: no 3D body was generated for {', '.join(missing)}.")
        target = pretty / footprint.name
        target.write_text(text, encoding="utf-8")
        footprints.append(target)
    register_library(project_dir, library)
    return footprints, models, notes


def _relative_models(text: str, by_name: dict[str, Path]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def _swap(match: re.Match[str]) -> str:
        name = Path(match.group(2)).name
        if name in by_name:
            return f'{match.group(1)}"${{KIPRJMOD}}/libs/3d/{name}"'
        missing.append(name)
        return match.group(0)

    return re.sub(r'(\(model\s+)"([^"]+)"', _swap, text), missing


def register_library(project_dir: Path, library: str) -> None:
    """Add ``library`` to the project's ``fp-lib-table`` once."""
    table = project_dir / "fp-lib-table"
    empty = "(fp_lib_table\n\t(version 7)\n)\n"
    text = table.read_text(encoding="utf-8") if table.exists() else empty
    if re.search(rf'\(name\s+"?{re.escape(library)}"?\)', text):
        return
    row = (
        f'\t(lib (name "{library}")(type "KiCad")(uri "${{KIPRJMOD}}/libs/{library}.pretty")'
        '(options "")(descr "Footprints generated for this project"))\n'
    )
    end = text.rstrip().rfind(")")
    table.write_text(text[:end].rstrip("\n") + "\n" + row + ")\n", encoding="utf-8")


# --- checks -----------------------------------------------------------------------------


def check(
    tools: Installed,
    footprint: Path,
    land_pattern: list[Pad] | None = None,
    tolerance_mm: float = 0.05,
) -> CheckResult:
    """KLC on the file, and every datasheet pad against the footprint's."""
    argv = [str(tools.python), "check_footprint.py", "-vv", "--nocolor", str(footprint)]
    code, text = _run(argv, cwd=tools.klc_check)
    if code == 1:
        raise LibraryToolsError(f"The KLC checker could not read {footprint.name}: {_tail(text)}")
    error_rules: set[str] = set()
    if code == 3:
        # ``-w`` hides warnings, so the rules it still prints are the ones with errors.
        _, errors_only = _run([*argv, "-w"], cwd=tools.klc_check)
        error_rules = {rule for rule, _ in _violations(errors_only)}
    result = CheckResult(footprint=footprint.stem)
    for rule, detail in _violations(text):
        line = f"{rule}: {detail}" if detail else rule
        if rule in NAMING_RULES:
            result.notes.append(line)
        elif rule in error_rules:
            result.klc_errors.append(line)
        else:
            result.klc_warnings.append(line)
    if land_pattern:
        pads = parse_pads(footprint.read_text(encoding="utf-8"))
        result.compared = len(land_pattern)
        result.mismatches = compare_pads(land_pattern, pads, tolerance_mm)
    return result


def _violations(text: str) -> list[tuple[str, str]]:
    """Each violated rule once, with its details (the checker repeats a rule's heading)."""
    details: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        match = re.match(r"\s*Violating\s+([A-Z]+\d+(?:\.\d+)?)", raw)
        if match:
            current = match.group(1)
            details.setdefault(current, [])
            continue
        line = raw.strip().lstrip("- ").strip()
        if current and raw.startswith("    ") and line and f" - {current} - " not in line:
            if line not in details[current]:
                details[current].append(line)
    return [(rule, "; ".join(lines)) for rule, lines in details.items()]


_PAD_RE = re.compile(
    r'\(pad\s+"(?P<num>[^"]*)"\s+(?P<kind>smd|thru_hole|np_thru_hole|connect)\s+\S+.*?'
    r"\(at\s+(?P<x>-?[\d.]+)\s+(?P<y>-?[\d.]+)(?:\s+(?P<rot>-?[\d.]+))?\s*\).*?"
    r"\(size\s+(?P<w>[\d.]+)\s+(?P<h>[\d.]+)\s*\)",
    re.DOTALL,
)


def parse_pads(text: str) -> list[Pad]:
    """Every numbered pad of a ``.kicad_mod`` (paste-only pads have no number)."""
    pads: list[Pad] = []
    for match in _PAD_RE.finditer(text):
        if not match.group("num"):
            continue
        pads.append(
            Pad(
                number=match.group("num"),
                x=float(match.group("x")),
                y=float(match.group("y")),
                w=float(match.group("w")),
                h=float(match.group("h")),
                rotation=float(match.group("rot") or 0.0),
            )
        )
    return pads


def compare_pads(expected: list[Pad], actual: list[Pad], tolerance_mm: float) -> list[str]:
    """Each datasheet pad against the footprint's nearest unpaired pad with that number.

    Pads may share a number (two mounting tabs ``MP``, an exposed pad with its thermal
    vias): each datasheet pad takes the closest footprint pad of its number not already
    taken, the larger on a tie, so two tabs are compared with two tabs.
    """
    mismatches: list[str] = []
    taken: set[int] = set()
    for want in expected:
        same = [i for i, p in enumerate(actual) if p.number == want.number and i not in taken]
        if not same:
            mismatches.append(f"pad {want.number}: not in the footprint")
            continue
        index = min(
            same,
            key=lambda i: (
                round(math.hypot(actual[i].x - want.x, actual[i].y - want.y), 6),
                -actual[i].w * actual[i].h,
            ),
        )
        taken.add(index)
        got = actual[index]
        w, h = (got.h, got.w) if round(got.rotation) % 180 == 90 else (got.w, got.h)
        problems = []
        if abs(got.x - want.x) > tolerance_mm or abs(got.y - want.y) > tolerance_mm:
            problems.append(
                f"at ({got.x:g}, {got.y:g}) mm, the datasheet ({want.x:g}, {want.y:g}) mm"
            )
        if abs(w - want.w) > tolerance_mm or abs(h - want.h) > tolerance_mm:
            problems.append(f"{w:g} x {h:g} mm, the datasheet {want.w:g} x {want.h:g} mm")
        if problems:
            mismatches.append(f"pad {want.number}: " + "; ".join(problems))
    numbers = {p.number for p in expected}
    extra = sorted({p.number for p in actual} - numbers)
    if expected and extra:
        mismatches.append(f"pads {', '.join(extra)}: in the footprint, not in the datasheet")
    return mismatches


# --- records ----------------------------------------------------------------------------


def update_record(project_dir: Path, name: str, **fields: object) -> dict[str, object]:
    """Merge ``fields`` into the footprint's record and return it."""
    path = project_dir / "libs" / RECORDS
    path.parent.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, object]] = {}
    if path.exists():
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            records = {}
    record = records.setdefault(name, {})
    record.update(fields)
    record["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def check_record(result: CheckResult, datasheet: str, tolerance_mm: float) -> dict[str, object]:
    return {
        "passed": result.passed,
        "datasheet": datasheet,
        "tolerance_mm": tolerance_mm,
        "compared_pads": result.compared,
        **{k: v for k, v in asdict(result).items() if k not in ("footprint", "compared")},
    }


# --- process ----------------------------------------------------------------------------


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    import os  # noqa: PLC0415

    full_env = {**os.environ, **(env or {})}
    try:
        result = run_to_end.run(
            argv, cwd=cwd, env=full_env, what=f"Library Tools {Path(argv[-1]).name}"
        )
    except OSError as exc:
        raise LibraryToolsError(f"Library Tools could not start ({exc}).") from exc
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _tail(text: str, lines: int = 25) -> str:
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:])
