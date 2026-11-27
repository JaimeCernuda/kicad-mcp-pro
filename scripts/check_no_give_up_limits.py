"""Fail on "finish in N seconds or fail" limits in the connector's source.

Nothing the connector runs gives up on its own (src/kicad_mcp/utils/run_to_end.py says
how long work has been running instead, and a stopped call stops it). A time-related line
is allowed only when it is not a give-up, said with ``limit-ok: <why>`` (or "not a limit")
on the line or the two lines above it: a poll tick that repeats, the stop's own grace
before forcing, a signed protocol expiry, a keep-alive.

    python scripts/check_no_give_up_limits.py        # exit 1 and list the lines
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src" / "kicad_mcp"
#: The model-benchmark harness, not the connector: its per-run limits are part of each
#: benchmark's definition and scoring (a decision for the person, recorded in the ledger).
EXCLUDED = ("evals/",)
PATTERNS = re.compile(
    r"timeout\s*=\s*(?!None\b|timeout\b|urllib3\.Timeout\(connect=None, read=None\))[^,)\s]"
    r"|\bdeadline\s*="
    r"|asyncio\.wait_for\("
    r"|asyncio\.timeout\("
    r"|TimeoutExpired"
    r"|timed out"
    r"|_timeout_s\b|timeout_seconds\s*[:=]",
)
MARKS = ("limit-ok:", "not a limit")


def findings(root: Path = ROOT) -> list[str]:
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(EXCLUDED):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            if not code or not PATTERNS.search(code):
                continue
            context = lines[max(0, number - 3) : number]
            if any(mark in text for text in context for mark in MARKS):
                continue
            out.append(f"src/kicad_mcp/{rel}:{number}: {line.strip()}")
    return out


def main() -> int:
    found = findings()
    for line in found:
        print(line)
    if found:
        print(
            f"\n{len(found)} time limit(s) without 'limit-ok: <why>'. Work runs to the end "
            "(utils/run_to_end.py); mark a line only when it is not a give-up."
        )
        return 1
    print("No give-up limits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
