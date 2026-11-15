"""Per-project revisions: every committed edit raises the number, whoever made it (KV6).

The tracker hashes what a view shows (the live board's text when KiCad serves it, else
the board file, plus the schematic files) and raises the revision when the hash moves.
Edits by the agent, by a view or by the person in KiCad's own window all move it.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _State:
    digest: str = ""
    revision: int = 0


class RevisionTracker:
    """Revision numbers keyed by project folder."""

    def __init__(self, fingerprint: Callable[[], tuple[str, str]]) -> None:
        """``fingerprint`` returns (project key, content digest) for the active project."""
        self._fingerprint = fingerprint
        self._lock = threading.Lock()
        self._states: dict[str, _State] = {}

    def current(self) -> int:
        """Hash the project now and return its revision (raised if it changed)."""
        key, digest = self._fingerprint()
        with self._lock:
            state = self._states.setdefault(key, _State())
            if digest != state.digest:
                state.digest = digest
                state.revision += 1
            return state.revision

    def wait(self, since: int, timeout_s: float, interval_s: float = 1.0) -> tuple[int, bool]:
        """Return as soon as the revision passes ``since``, or at the timeout."""
        deadline = time.monotonic() + max(0.0, min(timeout_s, 25.0))
        revision = self.current()
        while revision <= since and time.monotonic() < deadline:
            time.sleep(interval_s)
            revision = self.current()
        return revision, revision > since


def digest_of(*parts: bytes) -> str:
    """One SHA-256 over every part."""
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(len(part).to_bytes(8, "big"))
        hasher.update(part)
    return hasher.hexdigest()


def file_bytes(paths: list[Path]) -> list[bytes]:
    """Each file's bytes, or empty bytes for one that cannot be read."""
    out: list[bytes] = []
    for path in paths:
        try:
            out.append(path.read_bytes())
        except OSError:
            out.append(b"")
    return out
