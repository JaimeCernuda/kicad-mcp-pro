"""FastMCP-independent symbol and footprint catalog behavior."""

from __future__ import annotations

import difflib
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import get_config
from ..errors import ToolFailedError
from ..library_resolution import active_project_dir
from ..utils.library_tables import lib_table_paths, parse_lib_table, resolve_kicad_env
from ..utils.sexpr import _extract_block

_symbol_index: dict[str, dict[str, str]] | None = None
#: The libraries ``_symbol_index`` was built from; a different set (KiCad installed
#: after the server started, a project's sym-lib-table edited) means a rebuild.
_symbol_index_sources: tuple[tuple[str, str], ...] = ()
_symbol_index_lock = threading.Lock()


def symbol_library_dir() -> Path:
    """Return the configured KiCad symbol-library directory."""
    cfg = get_config()
    if cfg.symbol_library_dir is None or not cfg.symbol_library_dir.exists():
        raise FileNotFoundError("No KiCad symbol library directory is configured.")
    return cfg.symbol_library_dir


def symbol_library_files() -> dict[str, Path]:
    """Map every discoverable symbol-library nickname to its source file."""
    files: dict[str, Path] = {}
    cfg = get_config()
    if cfg.symbol_library_dir is not None and cfg.symbol_library_dir.exists():
        for sym_file in sorted(cfg.symbol_library_dir.glob("*.kicad_sym")):
            files.setdefault(sym_file.stem, sym_file)
    project_dir = active_project_dir()
    for table in lib_table_paths("sym-lib-table", project_dir):
        for nickname, sym_path in parse_lib_table(table, project_dir).items():
            files.setdefault(nickname, sym_path)
    return files


def symbol_sources() -> list[str]:
    """Where symbol libraries are looked for, and what each place holds right now."""
    cfg = get_config()
    folder = cfg.symbol_library_dir
    if folder is None:
        lines = ["KiCad's symbol folder: none is configured."]
    elif not folder.is_dir():
        lines = [f"KiCad's symbol folder {folder}: does not exist."]
    else:
        lines = [
            f"KiCad's symbol folder {folder}: {len(list(folder.glob('*.kicad_sym')))} libraries."
        ]
    project_dir = active_project_dir()
    project_table = project_dir / "sym-lib-table" if project_dir is not None else None
    if project_table is None:
        lines.append("Project sym-lib-table: no project is active.")
    elif not project_table.exists():
        lines.append(f"Project sym-lib-table {project_table}: does not exist.")
    tables = lib_table_paths("sym-lib-table", project_dir)
    for table in tables:
        kind = "Project" if table == project_table else "Global"
        lines.append(f"{kind} sym-lib-table {table}: {_table_state(table, project_dir)}")
    if all(table == project_table for table in tables):
        lines.append("Global sym-lib-table: none was found.")
    return lines


def _table_state(table: Path, project_dir: Path | None) -> str:
    try:
        content = table.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return f"cannot be read ({exc})."
    entries = 0
    missing: list[str] = []
    for chunk in re.split(r"\(lib\b", content)[1:]:
        name = re.search(r'\(name\s+"?([^")\s]+)"?\)', chunk)
        uri = re.search(r'\(uri\s+"([^"]+)"\)', chunk)
        if not (name and uri):
            continue
        entries += 1
        target = resolve_kicad_env(uri.group(1), project_dir)
        if not Path(target).exists():
            missing.append(f"{name.group(1)} -> {target}")
    state = f"{entries} libraries"
    if missing:
        state += f", {len(missing)} pointing at no file ({'; '.join(missing[:3])})"
    return state + "."


def _sources(files: dict[str, Path]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((library, str(path)) for library, path in files.items()))


def build_symbol_index(files: dict[str, Path] | None = None) -> dict[str, dict[str, str]]:
    """Build the in-memory search index from discoverable symbol libraries."""
    index: dict[str, dict[str, str]] = {}
    for library, sym_file in (symbol_library_files() if files is None else files).items():
        try:
            content = sym_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in re.finditer(r'\(symbol\s+"([^"]+)"', content):
            symbol_name = match.group(1)
            if re.search(r"_\d+_\d+$", symbol_name):
                continue
            key = f"{library}:{symbol_name}"
            description_match = re.search(
                rf'\(symbol\s+"{re.escape(symbol_name)}".*?\(property\s+"Description"\s+"([^"]*)"',
                content,
                re.DOTALL,
            )
            keyword_match = re.search(
                rf'\(symbol\s+"{re.escape(symbol_name)}".*?\(property\s+"ki_keywords"\s+"([^"]*)"',
                content,
                re.DOTALL,
            )
            index[key] = {
                "library": library,
                "name": symbol_name,
                "description": description_match.group(1) if description_match else "",
                "keywords": keyword_match.group(1) if keyword_match else "",
            }
    return index


def get_symbol_index() -> dict[str, dict[str, str]]:
    """Return the process-wide symbol search index, rebuilt when its libraries change."""
    global _symbol_index, _symbol_index_sources
    files = symbol_library_files()
    sources = _sources(files)
    with _symbol_index_lock:
        if _symbol_index is None or sources != _symbol_index_sources:
            _symbol_index = build_symbol_index(files)
            _symbol_index_sources = sources
        return _symbol_index


def rebuild_symbol_index() -> int:
    """Rebuild the process-wide symbol search index and return its size."""
    global _symbol_index, _symbol_index_sources
    files = symbol_library_files()
    with _symbol_index_lock:
        _symbol_index = build_symbol_index(files)
        _symbol_index_sources = _sources(files)
        return len(_symbol_index)


def read_symbol_file(library: str) -> str | None:
    """Read one discoverable symbol library, if present."""
    path = symbol_library_files().get(library)
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="ignore")


_PIN_START_RE = re.compile(r"\(pin\s+")


def _matching_symbols(
    index: dict[str, dict[str, str]], query: str, library_filter: str
) -> list[dict[str, str]]:
    # Split the query into whitespace-separated terms. A single-term query keeps the
    # original whole-query substring behavior; a multi-term query requires EVERY term
    # to appear (case-insensitive) somewhere in the combined name/description/keywords
    # haystack (AND across terms, OR across fields).
    terms = [term.lower() for term in query.split()]
    library_filter_lower = library_filter.lower()
    matches: list[dict[str, str]] = []
    for item in index.values():
        if library_filter_lower and item["library"].lower() != library_filter_lower:
            continue
        haystack = f"{item['name']} {item['description']} {item['keywords']}".lower()
        if all(term in haystack for term in terms):
            matches.append(item)
    return matches


def _render_symbol_match(item: dict[str, str]) -> str:
    parts = [f"- {item['library']}:{item['name']}"]
    alias = item.get("alias", "")
    description = item.get("description", "")
    keywords = item.get("keywords", "")
    if alias:
        parts.append(f" (alias: {alias})")
    if description:
        parts.append(f" - {description}")
    if keywords:
        parts.append(f" [keywords: {keywords}]")
    return "".join(parts)


def _extract_symbol_pins(block: str) -> list[tuple[str, str, str]]:
    """Extract pins from isolated balanced blocks, preserving duplicate numbers."""
    pins: list[tuple[str, str, str]] = []
    cursor = 0
    while True:
        match = _PIN_START_RE.search(block, cursor)
        if match is None:
            return pins
        pin_block, consumed = _extract_block(block, match.start())
        cursor = match.start() + max(consumed, 1)
        type_match = re.match(r"\(pin\s+(\w+)", pin_block)
        name_match = re.search(r'\(name\s+"([^"]*)"', pin_block)
        number_match = re.search(r'\(number\s+"([^"]*)"', pin_block)
        if type_match and name_match and number_match:
            pins.append((type_match.group(1), name_match.group(1), number_match.group(1)))


@dataclass(frozen=True)
class LibraryCatalogService:
    """Read/search symbol and footprint catalogs without FastMCP dependencies."""

    symbol_library_dir: Callable[[], Path]
    footprint_library_dirs: Callable[[], dict[str, Path]]
    get_symbol_index: Callable[[], dict[str, dict[str, str]]]
    read_symbol_file: Callable[[str], str | None]
    rebuild_symbol_index: Callable[[], int]
    footprint_file: Callable[[str, str], Path]
    max_items_per_response: Callable[[], int]

    def list_libraries(self) -> str:
        symbol_libs = sorted(path.stem for path in self.symbol_library_dir().glob("*.kicad_sym"))
        footprint_libs = sorted(f"{nickname}.pretty" for nickname in self.footprint_library_dirs())
        lines = [f"Symbol libraries ({len(symbol_libs)} total):"]
        lines.extend(f"- {name}" for name in symbol_libs[:50])
        lines.append("")
        lines.append(f"Footprint libraries ({len(footprint_libs)} total):")
        lines.extend(f"- {name}" for name in footprint_libs[:50])
        return "\n".join(lines)

    def search_symbols(
        self,
        query: str,
        library_filter: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> str:
        if page < 1:
            raise ToolFailedError("page must be >= 1.")
        if page_size < 1:
            raise ToolFailedError("page_size must be >= 1.")
        page_size = min(page_size, 500)
        results = _matching_symbols(self.get_symbol_index(), query, library_filter)
        total = len(results)
        if total == 0:
            return f"No symbols matched '{query}'."
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            raise ToolFailedError(f"Page {page} exceeds total pages ({total_pages}).")
        start = (page - 1) * page_size
        end = start + page_size
        page_results = results[start:end]
        lines = [
            f"Symbol matches for '{query}' "
            f"(page {page}/{total_pages}, {len(page_results)} shown, {total} total):"
        ]
        lines.extend(_render_symbol_match(item) for item in page_results)
        if end < total:
            lines.append(f"... and {total - end} more matches (use page={page + 1})")
        return "\n".join(lines)

    def _library_not_found(self, library: str, symbol_name: str) -> str:
        known = sorted({item["library"] for item in self.get_symbol_index().values()})
        lines = [
            f"Symbol library '{library}' was not found among the {len(known)} symbol "
            "libraries found in:",
            *(f"- {line}" for line in symbol_sources()),
        ]
        close = difflib.get_close_matches(library, known, n=5, cutoff=0.5)
        close += [name for name in known if library.lower() in name.lower() and name not in close]
        if close:
            lines.append("Close library names: " + ", ".join(close[:8]) + ".")
        lines.append(f"Call lib_search_symbols('{symbol_name}') to find which library holds it.")
        return "\n".join(lines)

    def get_symbol_info(self, library: str, symbol_name: str) -> str:
        content = self.read_symbol_file(library)
        if content is None:
            raise ToolFailedError(self._library_not_found(library, symbol_name))
        start = content.find(f'(symbol "{symbol_name}"')
        if start == -1:
            raise ToolFailedError(
                f"Symbol '{library}:{symbol_name}' was not found. "
                f"Call lib_search_symbols('{symbol_name}', library_filter='{library}') "
                "to list close names."
            )
        block, _ = _extract_block(content, start)
        description = re.search(r'\(property\s+"Description"\s+"([^"]*)"', block)
        keywords = re.search(r'\(property\s+"ki_keywords"\s+"([^"]*)"', block)
        datasheet = re.search(r'\(property\s+"Datasheet"\s+"([^"]*)"', block)
        footprint = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', block)
        pins = _extract_symbol_pins(block)
        if not pins:
            extends = re.search(r'\(extends\s+"([^"]+)"\)', block)
            if extends:
                parent_start = content.find(f'(symbol "{extends.group(1)}"')
                if parent_start != -1:
                    parent_block, _ = _extract_block(content, parent_start)
                    pins = _extract_symbol_pins(parent_block)
        lines = [f"Symbol: {library}:{symbol_name}"]
        if description:
            lines.append(f"- Description: {description.group(1)}")
        if keywords:
            lines.append(f"- Keywords: {keywords.group(1)}")
        if footprint:
            lines.append(f"- Default footprint: {footprint.group(1)}")
        if datasheet:
            lines.append(f"- Datasheet: {datasheet.group(1)}")
        if pins:
            lines.append(f"- Pins: {len(pins)}")
            for pin in pins:
                lines.append(f"  - {pin[2]} {pin[1]} ({pin[0]})")
        return "\n".join(lines)

    def get_datasheet_url(self, library: str, symbol_name: str) -> str:
        content = self.read_symbol_file(library)
        if content is None:
            raise ToolFailedError(self._library_not_found(library, symbol_name))
        match = re.search(
            rf'\(symbol\s+"{re.escape(symbol_name)}".*?\(property\s+"Datasheet"\s+"([^"]*)"',
            content,
            re.DOTALL,
        )
        if match is None or not match.group(1):
            return f"No datasheet URL was found for '{library}:{symbol_name}'."
        return match.group(1)

    def search_footprints(
        self,
        query: str,
        library_filter: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> str:
        if page < 1:
            raise ToolFailedError("page must be >= 1.")
        if page_size < 1:
            raise ToolFailedError("page_size must be >= 1.")
        page_size = min(page_size, 500)
        results: list[str] = []
        for nickname, library_dir in self.footprint_library_dirs().items():
            if library_filter and library_filter.lower() not in nickname.lower():
                continue
            for footprint in library_dir.glob("*.kicad_mod"):
                if query.lower() in footprint.stem.lower():
                    results.append(f"{nickname}:{footprint.stem}")
        total = len(results)
        if total == 0:
            return f"No footprints matched '{query}'."
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            raise ToolFailedError(f"Page {page} exceeds total pages ({total_pages}).")
        start = (page - 1) * page_size
        end = start + page_size
        page_results = results[start:end]
        lines = [
            f"Footprint matches for '{query}' "
            f"(page {page}/{total_pages}, {len(page_results)} shown, {total} total):"
        ]
        lines.extend(f"- {item}" for item in page_results)
        if end < total:
            lines.append(f"... and {total - end} more matches (use page={page + 1})")
        return "\n".join(lines)

    def list_footprints(self, library: str) -> str:
        library_dir = self.footprint_library_dirs().get(library)
        if library_dir is None or not library_dir.exists():
            raise ToolFailedError(f"Footprint library '{library}' was not found.")
        footprints = sorted(path.stem for path in library_dir.glob("*.kicad_mod"))
        lines = [f"Footprints in {library} ({len(footprints)} total):"]
        lines.extend(f"- {name}" for name in footprints[: self.max_items_per_response()])
        return "\n".join(lines)

    def rebuild_index(self) -> str:
        return f"Rebuilt the symbol index with {self.rebuild_symbol_index()} entries."

    def get_footprint_info(self, library: str, footprint: str) -> str:
        path = self.footprint_file(library, footprint)
        if not path.exists():
            raise ToolFailedError(f"Footprint '{library}:{footprint}' was not found.")
        content = path.read_text(encoding="utf-8", errors="ignore")
        model_match = re.search(r'\(model\s+"([^"]+)"', content)
        return "\n".join(
            [
                f"Footprint: {library}:{footprint}",
                f"- File: {path}",
                f"- 3D model: {model_match.group(1) if model_match else '(none)'}",
            ]
        )

    def get_footprint_3d_model(self, library: str, footprint: str) -> str:
        path = self.footprint_file(library, footprint)
        if not path.exists():
            raise ToolFailedError(f"Footprint '{library}:{footprint}' was not found.")
        content = path.read_text(encoding="utf-8", errors="ignore")
        model_match = re.search(r'\(model\s+"([^"]+)"', content)
        if model_match is None:
            return f"Footprint '{library}:{footprint}' does not define a 3D model."
        return model_match.group(1)
