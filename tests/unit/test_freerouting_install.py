"""FreeRouting is installed where the server runs, or the user is told how (no network here)."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from kicad_mcp.routing import freerouting_install as install


@pytest.fixture(autouse=True)
def _tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(install.TOOLS_ENV, str(tmp_path / "tools"))
    return tmp_path / "tools"


def test_the_jar_is_downloaded_once_and_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"a jar"
    monkeypatch.setattr(install, "JAR_SHA256", __import__("hashlib").sha256(payload).hexdigest())
    calls: list[str] = []

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(payload)

    monkeypatch.setattr(install.urllib.request, "urlopen", urlopen)
    notes: list[str] = []
    jar = install._ensure_jar(notes)
    assert jar.read_bytes() == payload and notes
    assert install._ensure_jar([]) == jar and len(calls) == 1  # cached: no second download


def test_a_jar_that_fails_its_checksum_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"x"))
    with pytest.raises(install.FreeRoutingInstallError, match="checksum"):
        install._ensure_jar([])
    assert not list((install.tools_home() / "freerouting").glob("*"))


def test_java_versions_are_read_old_and_new(monkeypatch: pytest.MonkeyPatch) -> None:
    for text, major in (('openjdk version "21.0.12.1"', 21), ('java version "1.8.0_402"', 8)):
        monkeypatch.setattr(
            install.subprocess,
            "run",
            lambda cmd, text_=text, **_k: subprocess.CompletedProcess(cmd, 0, "", text_),
        )
        assert install.java_major("java") == major


def test_an_old_java_gets_a_java_21_installed_without_admin_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/java")

    def fake_install(version: str, jre: bool, path: str) -> str:
        java = Path(path) / "jdk-21-jre" / "bin" / "java"
        java.parent.mkdir(parents=True)
        java.write_text("")
        return str(java.parent.parent)

    monkeypatch.setitem(sys.modules, "jdk", types.SimpleNamespace(install=fake_install))
    monkeypatch.setattr(
        install, "java_major", lambda java: 8 if str(java) == "/usr/bin/java" else 21
    )
    notes: list[str] = []
    java = install._ensure_java(notes)
    assert java.name == "java" and "jdk-21-jre" in str(java) and notes


def test_no_java_and_no_installer_says_how(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "jdk", None)  # import fails
    with pytest.raises(install.FreeRoutingInstallError) as exc_info:
        install._ensure_java([])
    assert "Java 21" in str(exc_info.value) and "adoptium" in str(exc_info.value)
