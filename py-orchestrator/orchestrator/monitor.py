import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional
import aiohttp

from .config import ROOT_DIR, TARGETS_FILE

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
DB_PATH = ROOT_DIR / "ops_state.db"


async def send_alert(
    session: aiohttp.ClientSession,
    event_type: str,
    message: str,
    details: Optional[dict] = None,
) -> None:
    if not ALERT_WEBHOOK_URL:
        return
    payload = {
        "content": f"**[{event_type}]** {message}",
        "text": f"[{event_type}] {message}",
        "event": event_type,
        "message": message,
        "details": details or {},
        "timestamp": int(time.time()),
    }
    try:
        async with session.post(
            ALERT_WEBHOOK_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=5),
        ):
            pass
    except Exception:
        pass


def print_telemetry_dashboard() -> None:
    print("=" * 74)
    print(" ZERRO-DEFI ($0-CAPITAL / $0-GAS) TELEMETRY & HEALTH DASHBOARD")
    print("=" * 74)

    if TARGETS_FILE.exists():
        try:
            tdata = json.loads(TARGETS_FILE.read_text())
            age = int(time.time()) - int(tdata.get("updated_at", 0))
            print(
                f"[Targets] Age={age}s | Morpho={len(tdata.get('morpho_targets', []))} | "
                f"Dutch={len(tdata.get('dutch_targets', []))} | Harvest={len(tdata.get('harvest_targets', []))}"
            )
        except Exception as e:
            print(f"[Targets] Read warning: {e}")
    else:
        print("[Targets] targets.json not yet generated.")

    if not DB_PATH.exists():
        print("[SQLite ] ops_state.db not initialized yet.")
        print("=" * 74)
        return

    with sqlite3.connect(DB_PATH) as conn:
        cb = conn.execute(
            "SELECT consecutive_failures, tripped_until, last_error FROM circuit_breaker WHERE id = 1"
        ).fetchone()
        if cb:
            failures, tripped_until, last_err = cb
            rem = max(0, tripped_until - int(time.time()))
            state_str = f"OPEN (cooldown {rem}s)" if rem > 0 else "CLOSED (Healthy)"
            print(f"[Circuit] State={state_str} | ConsecutiveFailures={failures} | LastErr={last_err}")

        pending = conn.execute(
            "SELECT user_op_hash, sender, nonce, bump_count, submitted_at FROM pending_user_ops"
        ).fetchall()
        print(f"[Pending] In-Flight UserOps={len(pending)}")
        for p in pending:
            print(
                f"  - OpHash={p[0][:18]}... Nonce={p[2]} Bumps={p[3]} Age={int(time.time()) - int(p[4])}s"
            )

        stats = conn.execute(
            """
            SELECT pipeline, status, COUNT(*), SUM(simulated_profit_usd), SUM(realized_profit_usd)
            FROM executions
            GROUP BY pipeline, status
            """
        ).fetchall()
        print("[PnL Summary by Pipeline & Status]")
        if not stats:
            print("  (No executions recorded yet)")
        for pipe, status, cnt, sim_sum, real_sum in stats:
            print(
                f"  - {pipe:18s} | {status:20s} | Count={cnt:4d} | "
                f"SimUSD=${(sim_sum or 0.0):.4f} | RealizedUSD=${(real_sum or 0.0):.4f}"
            )
    print("=" * 74)


if __name__ == "__main__":
    print_telemetry_dashboard()
