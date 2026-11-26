"""Install KiCad Library Tools where this server runs, or say how the user installs it.

KiCad Library Tools (gitlab.com/kicad/libraries/kicad-library-tools) generates KiCad's own
footprints and 3D models and holds the KLC checker. It is GPLv3, so this server never imports
or copies it: it clones the repository at a pinned commit into the tools folder, builds a
virtual environment there from the repository's own package definition, and runs its scripts
as child processes.

1. ``KICAD_MCP_LIBRARY_TOOLS`` points at a checkout the user made: it is used as given, with
   ``KICAD_MCP_LIBRARY_TOOLS_PYTHON`` as its interpreter when set.
2. Otherwise the pinned commit (``KICAD_MCP_LIBRARY_TOOLS_PIN``) is fetched with ``git`` into
   ``<tools>/library-tools/<pin>/repo`` and installed into ``.../venv``. The footprint side
   needs a few pure-Python packages; the 3D side adds CadQuery and is installed the first
   time a model is asked for.
3. When a step cannot complete (no ``git``, no network, no Python 3.12+), the error carries the
   reason and what the user does instead.

The tools folder is ``KICAD_MCP_TOOLS_HOME``, else ``<temp>/kicad-mcp-<user>/tools``.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - argv-only git, uv and pip, shell=False
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from ..routing.freerouting_install import tools_home

REPOSITORY = "https://gitlab.com/kicad/libraries/kicad-library-tools.git"
DEFAULT_PIN = "090252c1f2429ceb8bd8ef2aa466b6c135f6273a"
PIN_ENV = "KICAD_MCP_LIBRARY_TOOLS_PIN"
CHECKOUT_ENV = "KICAD_MCP_LIBRARY_TOOLS"
PYTHON_ENV = "KICAD_MCP_LIBRARY_TOOLS_PYTHON"
PYTHON_MIN = (3, 12)

_LOCK = threading.Lock()


class LibraryToolsInstallError(RuntimeError):
    """Library Tools could not be installed here; ``how_to`` says what the user does."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{reason} {how_to()}")


@dataclass(frozen=True)
class Installed:
    root: Path
    python: Path
    pin: str
    models: bool
    notes: tuple[str, ...] = ()

    @property
    def generators(self) -> Path:
        return self.root / "src" / "generators"

    @property
    def klc_check(self) -> Path:
        return self.root / "library_utils" / "klc-check"


def pin() -> str:
    return os.environ.get(PIN_ENV, "").strip() or DEFAULT_PIN


def how_to() -> str:
    """What the user does when the server cannot install Library Tools itself."""
    return (
        "To generate footprints and 3D models, clone KiCad Library Tools "
        f"({REPOSITORY}) at commit {pin()}, install it with Python 3.12 or later "
        "(`pip install -e <checkout>` for footprints, `pip install -e '<checkout>[3d]'` for "
        f"3D models), then set {CHECKOUT_ENV} to the checkout and {PYTHON_ENV} to that Python."
    )


def ensure(*, models: bool = False) -> Installed:
    """The Library Tools checkout and interpreter, installing what is missing."""
    with _LOCK:
        configured = os.environ.get(CHECKOUT_ENV, "").strip()
        if configured:
            return _configured(Path(configured).expanduser(), models=models)
        return _managed(models=models)


def _configured(root: Path, *, models: bool) -> Installed:
    if not (root / "src" / "generators" / "generate.py").is_file():
        raise LibraryToolsInstallError(
            f"{CHECKOUT_ENV} is {root}, which is not a Library Tools checkout."
        )
    python = Path(os.environ.get(PYTHON_ENV, "").strip() or sys.executable)
    if models and not _imports(python, "cadquery"):
        raise LibraryToolsInstallError(
            f"The Python for {root} ({python}) cannot import CadQuery, which 3D models need."
        )
    return Installed(root=root, python=python, pin="configured", models=models)


def _managed(*, models: bool) -> Installed:
    notes: list[str] = []
    commit = pin()
    base = tools_home() / "library-tools" / commit
    root = base / "repo"
    if not (root / "src" / "generators" / "generate.py").is_file():
        _clone(root, commit)
        notes.append(f"Fetched KiCad Library Tools {commit[:10]} into {root}.")
    python = _venv_python(base / "venv")
    if not python.exists():
        _make_venv(base / "venv")
        notes.append(f"Created its Python environment in {base / 'venv'}.")
    if not _imports(python, "KicadModTree"):
        _pip_install(python, str(root), "the footprint generators")
        notes.append("Installed the footprint generators' requirements.")
    if models and not _imports(python, "cadquery"):
        _pip_install(python, f"{root}[3d]", "the 3D model generators (CadQuery)")
        notes.append("Installed the 3D model generators' requirements (CadQuery).")
    return Installed(root=root, python=python, pin=commit, models=models, notes=tuple(notes))


def _run(argv: list[str], what: str, cwd: Path | None = None, timeout: int = 1800) -> None:
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LibraryToolsInstallError(f"{what} failed ({exc}).") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise LibraryToolsInstallError(f"{what} failed: {' '.join(tail)}")


def _clone(root: Path, commit: str) -> None:
    git = shutil.which("git")
    if git is None:
        raise LibraryToolsInstallError("Fetching KiCad Library Tools needs git, which is missing.")
    partial = root.with_name("repo.part")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    what = f"Fetching KiCad Library Tools {commit[:10]} from gitlab.com"
    _run([git, "init", "-q"], what, cwd=partial)
    _run([git, "fetch", "-q", "--depth", "1", REPOSITORY, commit], what, cwd=partial)
    _run([git, "checkout", "-q", "FETCH_HEAD"], what, cwd=partial)
    shutil.rmtree(root, ignore_errors=True)
    partial.replace(root)


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _make_venv(venv: Path) -> None:
    uv = shutil.which("uv")
    if uv is not None:
        argv = [uv, "venv", "-q", "--seed", "-p", ">=3.12", str(venv)]
        _run(argv, "Creating a Python 3.12+ environment")
        return
    if sys.version_info < PYTHON_MIN:
        raise LibraryToolsInstallError(
            "Library Tools needs Python 3.12 or later; this server runs "
            f"{sys.version_info.major}.{sys.version_info.minor} and uv is not available."
        )
    _run([sys.executable, "-m", "venv", str(venv)], "Creating a Python environment")


def _pip_install(python: Path, target: str, what: str) -> None:
    uv = shutil.which("uv")
    argv = (
        [uv, "pip", "install", "-q", "--python", str(python), "-e", target]
        if uv is not None
        else [str(python), "-m", "pip", "install", "-q", "-e", target]
    )
    _run(argv, f"Installing {what}")


def _imports(python: Path, module: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [str(python), "-c", f"import {module}"],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
