"""Run one full shadow cycle: index, simulate and render telemetry.

Never signs anything. Forces ``DRY_RUN=true`` before importing the orchestrator
so a stray ``.env`` value can never trigger a real submission.

    python scripts/run_live_shadow_cycle.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
ROOT_DIR = _SCRIPTS.parent
for _p in (str(ROOT_DIR), str(ROOT_DIR / "py-orchestrator"), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Set before importing the orchestrator: shadow runs can never submit.
os.environ["DRY_RUN"] = "true"

from console import ACCENT, ensure_utf8, heading, paint, status  # noqa: E402
from orchestrator.indexer import BaseStateIndexer  # noqa: E402
from orchestrator.monitor import print_telemetry_dashboard  # noqa: E402
from orchestrator.submitter import process_verified_outbox  # noqa: E402


async def main() -> int:
    ensure_utf8()
    heading("Zerro-DEFI", "shadow cycle")
    print()

    status("info", "rpc", "syncing targets from live Base Mainnet…")
    started = time.perf_counter()
    indexer = BaseStateIndexer()
    summary = await indexer.update_targets_file()
    status(
        "ok",
        "index",
        f"morpho {summary.get('morpho', 0)} · dutch {summary.get('dutch', 0)}"
        f" · harvest {summary.get('harvest', 0)} · {summary.get('duration_ms', 0)}ms",
    )

    status("info", "submit", "draining verified outbox in dry-run")
    await process_verified_outbox()

    print()
    print_telemetry_dashboard()
    print(paint(f"shadow cycle complete in {time.perf_counter() - started:.1f}s", ACCENT))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)