"""The connector has no "finish in N seconds or fail" limits (the give-up guard script)."""

from __future__ import annotations

import runpy
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_no_give_up_limits.py"


def test_no_time_limit_gives_up_on_work() -> None:
    findings = runpy.run_path(str(SCRIPT))["findings"]()
    assert findings == [], "\n".join(findings)
