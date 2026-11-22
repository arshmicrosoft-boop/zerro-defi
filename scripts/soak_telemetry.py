"""Aggregate a Zerro-DEFI shadow soak into JSON + Markdown evidence.

Reads the live heartbeats, targets.json, verified_outbox.json and the SQLite
state store, computes scan/soak totals and emits a machine-readable snapshot
plus a Markdown table block for OVERNIGHT_REPORT.md.

    python scripts/soak_telemetry.py            # JSON to stdout
    python scripts/soak_telemetry.py --md       # Markdown only
    python scripts/soak_telemetry.py --json OUT.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

_SCRIPTS = Path(__file__).resolve().parent
ROOT = _SCRIPTS.parent
ORCH = ROOT / "py-orchestrator"
for _p in (str(ROOT), str(ORCH), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from console import ACCENT, GREEN, MUTED, RED, ensure_utf8, heading, paint, panel  # noqa: E402,F401

RUNTIME = ROOT / "runtime"
ORCH_STATUS = RUNTIME / "orchestrator_status.json"
SNIPER_STATUS = RUNTIME / "sniper_status.json"
TARGETS = ROOT / "targets.json"
OUTBOX = ROOT / "verified_outbox.json"
DB = ROOT / "ops_state.db"


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _read_list(path: Path) -> list:
    try:
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return []
            data = json.loads(raw)
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _read_db() -> dict:
    out = {
        "available": DB.exists(),
        "executions": 0,
        "confirmed": 0,
        "failed": 0,
        "pending": 0,
        "failures": 0,
        "tripped_until": 0,
        "last_error": "",
        "rows": [],
    }
    if not DB.exists():
        return out
    try:
        with sqlite3.connect(DB) as conn:
            conn.row_factory = sqlite3.Row
            out["executions"] = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
            out["confirmed"] = conn.execute(
                "SELECT COUNT(*) FROM executions WHERE status LIKE 'CONFIRMED%'"
            ).fetchone()[0]
            out["failed"] = conn.execute(
                "SELECT COUNT(*) FROM executions WHERE status LIKE 'FAILED%'"
            ).fetchone()[0]
            out["pending"] = conn.execute("SELECT COUNT(*) FROM pending_user_ops").fetchone()[0]
            cb = conn.execute(
                "SELECT consecutive_failures, tripped_until, last_error FROM circuit_breaker WHERE id = 1"
            ).fetchone()
            if cb:
                out["failures"] = int(cb["consecutive_failures"] or 0)
                out["tripped_until"] = int(cb["tripped_until"] or 0)
                out["last_error"] = cb["last_error"] or ""
            out["rows"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT pipeline, status, simulated_profit_usd, realized_profit_usd, created_at "
                    "FROM executions ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
            ]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def collect() -> dict:
    now = time.time()
    orch = _read_json(ORCH_STATUS)
    sniper = _read_json(SNIPER_STATUS)
    targets = _read_json(TARGETS)
    outbox = _read_list(OUTBOX)
    db = _read_db()

    started = float(orch.get("started_at", 0) or 0) or now
    soak_seconds = int(now - started) if started else 0
    sniper_started = now - float(sniper.get("uptime_sec", 0) or 0)

    sync_count = int(orch.get("sync_count", 0) or 0)
    sync_interval = 60
    blocks_covered_est = int(sync_count * sync_interval / 2)  # ~2s Base blocks

    morpho = len(targets.get("morpho_targets") or [])
    dutch = len(targets.get("dutch_targets") or [])
    harvest = len(targets.get("harvest_targets") or [])

    outbox_count = len(outbox)
    verified_total = int(sniper.get("verified", 0) or 0)
    sims_total = int(sniper.get("simulated", 0) or 0)
    cycles = int(sniper.get("cycles", 0) or 0)

    return {
        "collected_at": int(now),
        "soak": {
            "started_at": int(started),
            "seconds": soak_seconds,
            "sniper_uptime_sec": int(sniper.get("uptime_sec", 0) or 0),
            "sniper_start_est": int(sniper_started),
            "mode": orch.get("mode", "dry-run"),
            "chain_id": orch.get("chain_id", 8453),
        },
        "orchestrator": {
            "healthy": bool(orch.get("healthy")),
            "sync_count": sync_count,
            "last_sync_ms": int(orch.get("last_sync_ms", 0) or 0),
            "last_sync": orch.get("last_sync") or {},
            "outbox": int(orch.get("outbox", 0) or 0),
            "pending": int(orch.get("pending", 0) or 0),
            "circuit_open": bool(orch.get("circuit_open")),
            "error": orch.get("error", ""),
        },
        "sniper": {
            "block": int(sniper.get("block", 0) or 0),
            "cycles": cycles,
            "simulated": sims_total,
            "verified": verified_total,
            "best_profit_usd": float(sniper.get("best_profit_usd", 0) or 0),
            "latency_ms": int(sniper.get("latency_ms", 0) or 0),
            "stale": bool(sniper.get("stale")),
            "rpc": sniper.get("rpc", ""),
            "error": sniper.get("error", ""),
            "morpho": int(sniper.get("morpho", 0) or 0),
            "dutch": int(sniper.get("dutch", 0) or 0),
            "harvest": int(sniper.get("harvest", 0) or 0),
        },
        "targets": {
            "updated_at": int(targets.get("updated_at", 0) or 0),
            "morpho": morpho,
            "dutch": dutch,
            "harvest": harvest,
            "total": morpho + dutch + harvest,
        },
        "outbox": outbox_count,
        "blocks_covered_est": blocks_covered_est,
        "db": {
            "executions": db["executions"],
            "confirmed": db["confirmed"],
            "failed": db["failed"],
            "pending": db["pending"],
            "failures": db["failures"],
            "tripped_until": db["tripped_until"],
            "last_error": db["last_error"],
        },
        "verdict": {
            "soak_clean": db["failures"] == 0 and db["executions"] == 0,
            "sniper_healthy": not bool(sniper.get("stale")) and not bool(sniper.get("error")),
            "orchestrator_healthy": bool(orch.get("healthy")),
        },
    }


def markdown(data: dict) -> str:
    s = data["soak"]
    o = data["orchestrator"]
    sn = data["sniper"]
    t = data["targets"]
    db = data["db"]
    v = data["verdict"]
    lines = [
        f"- **Collected:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['collected_at']))}",
        f"- **Mode:** {s['mode']} · Base chain_id {s['chain_id']}",
        f"- **Soak duration:** {s['seconds']}s (~{s['seconds'] // 3600}h{(s['seconds'] % 3600) // 60:02d}m)",
        f"- **Orchestrator syncs:** {o['sync_count']} · last {o['last_sync_ms']}ms · healthy={o['healthy']}",
        f"- **Sniper cycles:** {sn['cycles']} · sims {sn['simulated']:,} · verified {sn['verified']} · best ${sn['best_profit_usd']:.4f}",
        f"- **Sniper block:** {sn['block']:,} · latency {sn['latency_ms']}ms · stale={sn['stale']} · rpc={sn['rpc']}",
        f"- **Targets:** {t['total']} armed (morpho {t['morpho']} · dutch {t['dutch']} · harvest {t['harvest']})",
        f"- **Est. blocks covered:** ~{data['blocks_covered_est']:,} (60s indexer cadence × ~2s blocks)",
        f"- **Outbox:** {data['outbox']} pending · DB executions {db['executions']} (confirmed {db['confirmed']}, failed {db['failed']})",
        f"- **Circuit:** failures={db['failures']} · tripped_until={db['tripped_until']} · last_error={db['last_error'] or '—'}",
        f"- **Verdict:** soak_clean={v['soak_clean']} · sniper_healthy={v['sniper_healthy']} · orchestrator_healthy={v['orchestrator_healthy']}",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="soak_telemetry")
    parser.add_argument("--json", metavar="OUT", help="write JSON snapshot to file")
    parser.add_argument("--md", action="store_true", help="print Markdown block instead of JSON")
    args = parser.parse_args(argv)

    ensure_utf8()
    data = collect()

    if args.json:
        Path(args.json).write_text(json.dumps(data, indent=2), encoding="utf-8")
    if args.md:
        print(markdown(data))
    elif not args.json:
        print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())