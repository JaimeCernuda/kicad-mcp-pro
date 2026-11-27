"""Install FreeRouting where this server runs, or say how the user installs it.

FreeRouting is a Java program: it needs its JAR and a Java 21 runtime. Docker is one way to
get both but is often missing or not running, so the runner asks this module first:

1. a JAR the user configured (``KICAD_MCP_FREEROUTING_JAR``) is used as given;
2. otherwise the pinned release JAR is downloaded once into the tools folder and checked
   against its SHA-256;
3. ``java`` on ``PATH`` is used when it is Java 21 or later; otherwise a Java 21 runtime
   (Eclipse Temurin) is installed into the tools folder with the ``install-jdk`` package,
   when it is installed (no administrator rights are needed);
4. when a step cannot complete (no network, no ``install-jdk``), :class:`FreeRoutingInstallError`
   carries the reason and what the user does instead.

The tools folder is ``KICAD_MCP_TOOLS_HOME``, else ``<temp>/kicad-mcp-<user>/tools``.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import shutil
import subprocess  # nosec B404 - argv-only java version probe, shell=False
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..utils import http_to_end, run_to_end

VERSION = "2.1.0"
JAR_URL = (
    f"https://github.com/freerouting/freerouting/releases/download/v{VERSION}/"
    f"freerouting-{VERSION}.jar"
)
JAR_SHA256 = "2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def"
JAVA_MAJOR = 21
TOOLS_ENV = "KICAD_MCP_TOOLS_HOME"


class FreeRoutingInstallError(RuntimeError):
    """FreeRouting could not be installed here; ``how_to`` says what the user does."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{reason} {how_to()}")


@dataclass(frozen=True)
class Installed:
    jar: Path
    java: Path
    notes: tuple[str, ...] = ()


def how_to() -> str:
    """What the user does when the server cannot install FreeRouting itself."""
    return (
        f"To autoroute, install a Java {JAVA_MAJOR} runtime (https://adoptium.net) and the "
        f"FreeRouting {VERSION} JAR ({JAR_URL}), then set KICAD_MCP_FREEROUTING_JAR to the "
        "JAR; or start Docker, which runs the FreeRouting image. Until then, route with the "
        "track tools."
    )


def tools_home() -> Path:
    raw = os.environ.get(TOOLS_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (a container without a passwd entry)
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"kicad-mcp-{user}" / "tools"


def ensure(configured_jar: Path | None = None) -> Installed:
    """The JAR and Java to run FreeRouting with, installing what is missing."""
    notes: list[str] = []
    jar = configured_jar if configured_jar is not None else _ensure_jar(notes)
    if not jar.exists():
        raise FreeRoutingInstallError(f"The configured FreeRouting JAR {jar} does not exist.")
    return Installed(jar=jar, java=_ensure_java(notes), notes=tuple(notes))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _ensure_jar(notes: list[str]) -> Path:
    target = tools_home() / "freerouting" / f"freerouting-{VERSION}.jar"
    if target.exists() and _sha256(target) == JAR_SHA256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    try:
        with partial.open("wb") as out:  # the download takes as long as it takes (http_to_end)
            http_to_end.fetch(
                JAR_URL,
                what="Downloading FreeRouting",
                opener=lambda req: urllib.request.urlopen(req, timeout=None),  # noqa: S310  # nosec B310 - fixed https URL
                sink=out.write,
            )
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise FreeRoutingInstallError(
            f"The FreeRouting JAR could not be downloaded ({exc})."
        ) from exc
    if _sha256(partial) != JAR_SHA256:
        partial.unlink(missing_ok=True)
        raise FreeRoutingInstallError("The downloaded FreeRouting JAR did not match its checksum.")
    partial.replace(target)
    notes.append(f"Installed FreeRouting {VERSION} into {target.parent}.")
    return target


def java_major(java: Path | str) -> int:
    """The major version a ``java`` reports, or 0 when it cannot run."""
    try:
        result = run_to_end.run([str(java), "-version"], what="java -version")
    except (OSError, subprocess.SubprocessError):
        return 0
    match = re.search(r'version "(\d+)(?:\.(\d+))?', result.stderr + result.stdout)
    if not match:
        return 0
    major = int(match.group(1))
    return int(match.group(2) or 0) if major == 1 else major


def _installed_java() -> Path | None:
    for candidate in sorted((tools_home() / "java").glob("*/bin/java*")):
        if candidate.name in ("java", "java.exe") and java_major(candidate) >= JAVA_MAJOR:
            return candidate
    for candidate in sorted((tools_home() / "java").glob("*/Contents/Home/bin/java")):
        if java_major(candidate) >= JAVA_MAJOR:
            return candidate
    return None


def _ensure_java(notes: list[str]) -> Path:
    system = shutil.which("java")
    if system is not None and java_major(system) >= JAVA_MAJOR:
        return Path(system)
    installed = _installed_java()
    if installed is not None:
        return installed
    try:
        import jdk  # noqa: PLC0415 - optional: the install-jdk package
    except ImportError as exc:
        found = f"Java {java_major(system)}" if system else "no Java"
        raise FreeRoutingInstallError(
            f"FreeRouting needs Java {JAVA_MAJOR}; this machine has {found}, and the "
            "install-jdk package that installs one without administrator rights is missing."
        ) from exc
    folder = tools_home() / "java"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        jdk.install(str(JAVA_MAJOR), jre=True, path=str(folder))
    except Exception as exc:  # noqa: BLE001 - network, archive or platform: reported with the cause
        raise FreeRoutingInstallError(
            f"A Java {JAVA_MAJOR} runtime could not be installed ({exc})."
        ) from exc
    installed = _installed_java()
    if installed is None:
        raise FreeRoutingInstallError(
            f"A Java {JAVA_MAJOR} runtime was installed but did not start."
        )
    notes.append(f"Installed a Java {JAVA_MAJOR} runtime into {folder}.")
    return installed
