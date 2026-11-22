"""Live telemetry dashboard for the Zerro-DEFI stack.

Reads the on-disk service heartbeats written by the orchestrator and the Rust
sniper, queries Base Mainnet for chain/wallet state, then paints a single
flicker-free dashboard frame.

    python -m orchestrator.monitor              # live dashboard
    python -m orchestrator.monitor --once       # one snapshot, then exit
    python -m orchestrator.monitor --json       # machine readable snapshot
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import aiohttp
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import to_checksum_address

from . import ui
from .config import (
    BASE_RPC_URLS,
    EXECUTOR_ADDRESS,
    MULTICALL3,
    ROOT_DIR,
    TARGETS_FILE,
    USDC_BASE,
    WETH_BASE,
    VERIFIED_OUTBOX_FILE,
)

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
DB_PATH = ROOT_DIR / "ops_state.db"
RUNTIME_DIR = ROOT_DIR / "runtime"
ORCH_STATUS = RUNTIME_DIR / "orchestrator_status.json"
SNIPER_STATUS = RUNTIME_DIR / "sniper_status.json"

SEL_BALANCE_OF = bytes.fromhex("70a08231")
SEL_TRY_AGGREGATE = bytes.fromhex("bce38bd7")


# ---------------------------------------------------------------------------
# Alerts (kept for the submitter)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Disk state
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def read_targets() -> dict:
    data = _read_json(TARGETS_FILE)
    return {
        "updated_at": int(data.get("updated_at", 0) or 0),
        "morpho": len(data.get("morpho_targets", []) or []),
        "dutch": len(data.get("dutch_targets", []) or []),
        "harvest": len(data.get("harvest_targets", []) or []),
    }


def read_outbox_count() -> int:
    try:
        if VERIFIED_OUTBOX_FILE.exists():
            raw = VERIFIED_OUTBOX_FILE.read_text(encoding="utf-8").strip()
            if not raw:
                return 0
            data = json.loads(raw)
            return len(data) if isinstance(data, list) else 0
    except Exception:
        pass
    return 0


def read_db() -> dict:
    snapshot: Dict[str, object] = {
        "available": DB_PATH.exists(),
        "failures": 0,
        "tripped_until": 0,
        "last_error": "",
        "pending": [],
        "rows": [],
    }
    if not DB_PATH.exists():
        return snapshot
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cb = conn.execute(
                "SELECT consecutive_failures, tripped_until, last_error FROM circuit_breaker WHERE id = 1"
            ).fetchone()
            if cb:
                snapshot["failures"] = int(cb["consecutive_failures"] or 0)
                snapshot["tripped_until"] = int(cb["tripped_until"] or 0)
                snapshot["last_error"] = cb["last_error"] or ""
            pending = conn.execute(
                "SELECT user_op_hash, nonce, bump_count, submitted_at FROM pending_user_ops"
            ).fetchall()
            snapshot["pending"] = [
                {
                    "hash": row["user_op_hash"],
                    "nonce": row["nonce"],
                    "bumps": int(row["bump_count"] or 0),
                    "age": int(time.time()) - int(row["submitted_at"] or 0),
                }
                for row in pending
            ]
            rows = conn.execute(
                """
                SELECT pipeline, description, status, simulated_profit_usd,
                       realized_profit_usd, created_at
                FROM executions ORDER BY created_at DESC LIMIT 400
                """
            ).fetchall()
            snapshot["rows"] = [dict(row) for row in rows]
    except Exception as exc:  # pragma: no cover - defensive
        snapshot["error"] = str(exc)
    return snapshot


def aggregate_pnl(rows: List[dict]) -> List[dict]:
    buckets: Dict[str, dict] = {}
    for row in rows:
        pipe = str(row.get("pipeline") or "unknown")
        bucket = buckets.setdefault(
            pipe,
            {"pipeline": pipe, "total": 0, "confirmed": 0, "failed": 0,
             "sim": 0.0, "realized": 0.0},
        )
        status = str(row.get("status") or "")
        bucket["total"] += 1
        bucket["sim"] += float(row.get("simulated_profit_usd") or 0.0)
        if status.startswith("CONFIRMED"):
            bucket["confirmed"] += 1
            bucket["realized"] += float(row.get("realized_profit_usd") or 0.0)
        elif status.startswith("FAILED"):
            bucket["failed"] += 1
    return sorted(buckets.values(), key=lambda b: b["pipeline"])


# ---------------------------------------------------------------------------
# Live chain state
# ---------------------------------------------------------------------------


async def _rpc(
    session: aiohttp.ClientSession, url: str, method: str, params: list, timeout: float = 5.0
):
    async with session.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        return await resp.json(content_type=None)


async def _rpc_any(session: aiohttp.ClientSession, method: str, params: list):
    for url in BASE_RPC_URLS:
        try:
            body = await _rpc(session, url, method, params)
            if isinstance(body, dict) and body.get("result") is not None:
                return body["result"]
        except Exception:
            continue
    return None


async def fetch_chain_state(session: aiohttp.ClientSession) -> dict:
    """Fetch block head, gas, executor status and wallet balances in parallel."""
    state: Dict[str, object] = {
        "ok": False,
        "block": 0,
        "block_ts": 0,
        "gas_wei": 0,
        "base_fee_wei": 0,
        "latency_ms": 0,
        "executor_deployed": False,
        "wallet": "",
        "usdc": None,
        "weth": None,
        "eth": None,
        "error": "",
    }
    wallet = (
        os.getenv("SMART_ACCOUNT_ADDRESS", "").strip()
        or os.getenv("EXECUTOR_ADDRESS", "").strip()
        or EXECUTOR_ADDRESS
    )
    if wallet.lower() in ("", "0x" + "0" * 40):
        wallet = ""
    state["wallet"] = wallet

    started = time.perf_counter()
    try:
        block_hex = await _rpc_any(session, "eth_blockNumber", [])
        state["latency_ms"] = int((time.perf_counter() - started) * 1000)
        if not block_hex:
            state["error"] = "rpc unreachable"
            return state
        block = int(block_hex, 16)
        state["block"] = block

        async def block_detail():
            return await _rpc_any(session, "eth_getBlockByNumber", [hex(block), False])

        async def gas_price():
            return await _rpc_any(session, "eth_gasPrice", [])

        async def executor_code():
            if not EXECUTOR_ADDRESS or EXECUTOR_ADDRESS.lower() == "0x" + "0" * 40:
                return None
            return await _rpc_any(session, "eth_getCode", [EXECUTOR_ADDRESS, "latest"])

        async def balances():
            if not wallet:
                return None
            calls = [
                (to_checksum_address(USDC_BASE), SEL_BALANCE_OF + abi_encode(["address"], [to_checksum_address(wallet)])),
                (to_checksum_address(WETH_BASE), SEL_BALANCE_OF + abi_encode(["address"], [to_checksum_address(wallet)])),
            ]
            calldata = "0x" + (
                SEL_TRY_AGGREGATE
                + abi_encode(["bool", "(address,bytes)[]"], [False, calls])
            ).hex()
            result = await _rpc_any(session, "eth_call", [{"to": MULTICALL3, "data": calldata}, "latest"])
            eth = await _rpc_any(session, "eth_getBalance", [wallet, "latest"])
            decoded: List[Tuple[bool, bytes]] = []
            if result and result != "0x":
                try:
                    raw = abi_decode(["(bool,bytes)[]"], bytes.fromhex(result[2:]))[0]
                    decoded = [(bool(ok), bytes(payload)) for ok, payload in raw]
                except Exception:
                    decoded = []
            usdc = None
            weth = None
            if len(decoded) == 2:
                if decoded[0][0] and len(decoded[0][1]) >= 32:
                    usdc = int(abi_decode(["uint256"], decoded[0][1][:32])[0]) / 1e6
                if decoded[1][0] and len(decoded[1][1]) >= 32:
                    weth = int(abi_decode(["uint256"], decoded[1][1][:32])[0]) / 1e18
            eth_val = int(eth, 16) / 1e18 if eth else None
            return (usdc, weth, eth_val)

        detail, gas, code, bal = await asyncio.gather(
            block_detail(), gas_price(), executor_code(), balances(),
            return_exceptions=True,
        )

        if isinstance(detail, dict) and detail:
            state["block_ts"] = int(detail.get("timestamp", "0x0"), 16)
            base_fee = detail.get("baseFeePerGas")
            if base_fee:
                state["base_fee_wei"] = int(base_fee, 16)
        if isinstance(gas, str) and gas:
            state["gas_wei"] = int(gas, 16)
        if isinstance(code, str):
            state["executor_deployed"] = code not in ("0x", "0x0", "")
        if isinstance(bal, tuple):
            state["usdc"], state["weth"], state["eth"] = bal
        state["ok"] = True
    except Exception as exc:  # pragma: no cover - network dependent
        state["error"] = str(exc)[:80]
    return state


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _svc(label: str, state: str, detail: str, width: int) -> str:
    color = {
        "RUNNING": ui.GREEN, "OK": ui.GREEN, "CLOSED": ui.GREEN,
        "IDLE": ui.MUTED, "WAITING": ui.MUTED, "OFFLINE": ui.FAINT,
        "STALE": ui.AMBER, "DEGRADED": ui.AMBER, "PAUSED": ui.AMBER,
        "OPEN": ui.RED, "ERROR": ui.RED, "STOPPED": ui.FAINT,
    }.get(state, ui.MUTED)
    head = ui.paint(ui.glyph("dot"), color) + " " + ui.paint(f"{label:<10}", ui.INK, bold=True)
    chip = ui.paint(f"{state:<9}", color, bold=True)
    remaining = width - ui.vlen(head) - ui.vlen(chip) - 1
    return head + chip + ui.paint(ui.truncate(detail, max(0, remaining)), ui.MUTED)


def render(state: dict) -> List[str]:
    cols, _rows = ui.term_size()
    width = max(78, min(cols - 1, 116))

    chain = state["chain"]
    targets = state["targets"]
    db = state["db"]
    orch = state["orch"]
    sniper = state["sniper"]
    outbox = state["outbox"]
    now = time.time()

    live = chain.get("ok")
    frame = ui.Frame(
        width,
        title="ZERRO-DEFI  ·  TELEMETRY",
        right=f"{ui.glyph('dot')} LIVE  {ui.now_clock()}" if live else f"{ui.glyph('cross')} RPC DOWN  {ui.now_clock()}",
        accent=ui.ACCENT,
    )

    # -- chain -------------------------------------------------------------
    if live:
        block_age = int(now - chain["block_ts"]) if chain["block_ts"] else 0
        left = (
            ui.paint("BASE", ui.ACCENT, bold=True)
            + ui.paint(" 8453", ui.FAINT)
            + ui.paint("  ·  block ", ui.MUTED)
            + ui.paint(ui.human_int(chain["block"]), ui.INK, bold=True)
            + ui.paint(f"  ·  +{block_age}s", ui.MUTED)
            + ui.paint("  ·  gas ", ui.MUTED)
            + ui.paint(ui.gwei(chain["gas_wei"]), ui.SKY)
        )
        right = (
            ui.paint(f"rpc {chain['latency_ms']}ms", ui.MUTED)
            + " "
            + ui.sparkline(state["latency_hist"], ui.SKY)
        )
    else:
        left = ui.paint("BASE 8453", ui.ACCENT, bold=True) + ui.paint(
            f"  ·  {chain.get('error') or 'connecting…'}", ui.RED
        )
        right = ui.paint("retrying", ui.MUTED)
    frame.pair(left, right)

    frame.rule("SERVICES")

    # Indexer health
    if orch:
        age = int(now - float(orch.get("updated_at", 0) or 0))
        t_age = int(now - targets["updated_at"]) if targets["updated_at"] else -1
        if age <= 10:
            svc = "RUNNING"
        elif age <= 45:
            svc = "DEGRADED"
        else:
            svc = "STALE"
        detail = (
            f"sync {t_age}s ago · {targets['morpho'] + targets['dutch'] + targets['harvest']} targets"
            f" · cycle #{orch.get('sync_count', 0)}"
        )
    else:
        svc = "OFFLINE"
        detail = "no heartbeat · start orchestrator.main"
    frame.row(_svc("Indexer", svc, detail, width - 4))

    # Sniper health
    if sniper:
        age = int(now - float(sniper.get("updated_at", 0) or 0))
        svc = "RUNNING" if age <= 5 else ("DEGRADED" if age <= 20 else "STALE")
        if sniper.get("stale"):
            svc = "STALE"
        detail = (
            f"block {ui.human_int(sniper.get('block', 0))} · "
            f"{sniper.get('simulated', 0)} sims · {sniper.get('verified', 0)} hits"
            f" · {sniper.get('latency_ms', 0)}ms"
        )
    else:
        svc = "OFFLINE"
        detail = "no heartbeat · start zero-cap-sniper"
    frame.row(_svc("Sniper", svc, detail, width - 4))

    # Submitter health
    tripped = int(db.get("tripped_until") or 0)
    if tripped > now:
        svc = "OPEN"
        detail = f"cooldown {ui.human_secs(tripped - now)} · failures {db.get('failures', 0)}"
    elif db.get("pending"):
        svc = "WAITING"
        detail = f"{len(db['pending'])} UserOp in flight · outbox {outbox}"
    else:
        svc = "CLOSED"
        detail = f"circuit closed · outbox {outbox} · failures {db.get('failures', 0)}"
    frame.row(_svc("Submitter", svc, detail, width - 4))

    # -- targets -----------------------------------------------------------
    frame.rule("TARGETS")
    total = targets["morpho"] + targets["dutch"] + targets["harvest"]
    max_count = max(1, targets["morpho"], targets["dutch"], targets["harvest"])
    for label, count, color in (
        ("Morpho", targets["morpho"], ui.VIOLET),
        ("Dutch", targets["dutch"], ui.SKY),
        ("Harvest", targets["harvest"], ui.ACCENT),
    ):
        bar = ui.meter(count / max_count if count else 0, 18, color)
        frame.pair(
            f"{ui.paint(f'{label:<8}', ui.MUTED)} {ui.paint(f'{count:>4}', ui.INK, bold=True)}  {bar}",
            ui.paint(f"age {ui.ago(targets['updated_at']) if targets['updated_at'] else '—'}", ui.FAINT),
        )
    frame.pair(
        ui.paint(f"{'Total':<8}", ui.MUTED) + " " + ui.paint(f"{total:>4}", ui.ACCENT, bold=True)
        + ui.paint("  targets armed", ui.FAINT),
        ui.paint(f"executor {'deployed' if chain.get('executor_deployed') else 'simulated'}", ui.FAINT),
    )

    # -- wallet ------------------------------------------------------------
    frame.rule("WALLET")
    wallet = chain.get("wallet") or ""
    usdc = chain.get("usdc")
    weth = chain.get("weth")
    eth = chain.get("eth")
    frame.pair(
        ui.paint("USDC ", ui.MUTED) + ui.paint(f"{usdc:,.2f}" if usdc is not None else "—", ui.GREEN, bold=True)
        + ui.paint("   WETH ", ui.MUTED) + ui.paint(f"{weth:.6f}" if weth is not None else "—", ui.SKY, bold=True)
        + ui.paint("   ETH ", ui.MUTED) + ui.paint(f"{eth:.6f}" if eth is not None else "—", ui.VIOLET, bold=True),
        ui.paint(ui.short_addr(wallet, 8, 6) if wallet else "no smart account", ui.FAINT),
    )

    # -- pnl ---------------------------------------------------------------
    frame.rule("PnL")
    header = (
        ui.paint(f"{'PIPELINE':<12}", ui.FAINT)
        + ui.paint(f"{'TRY':>4}", ui.FAINT)
        + ui.paint(f"{'OK':>4}", ui.FAINT)
        + ui.paint(f"{'FAIL':>5}", ui.FAINT)
        + ui.paint(f"{'SIM':>10}", ui.FAINT)
        + ui.paint(f"{'REALIZED':>11}", ui.FAINT)
    )
    frame.row(header)
    pnl = aggregate_pnl(db.get("rows", []))
    if not pnl:
        frame.row(ui.paint("no executions recorded yet — shadow soak is clean", ui.FAINT))
    for row in pnl[:5]:
        line = (
            ui.paint(f"{row['pipeline'][:12]:<12}", ui.INK)
            + ui.paint(f"{row['total']:>4}", ui.MUTED)
            + ui.paint(f"{row['confirmed']:>4}", ui.GREEN if row["confirmed"] else ui.FAINT)
            + ui.paint(f"{row['failed']:>5}", ui.RED if row["failed"] else ui.FAINT)
            + ui.paint(f"{ui.usd(row['sim'], 2):>10}", ui.MUTED)
            + ui.paint(f"{ui.usd(row['realized'], 2):>11}", ui.GREEN if row["realized"] else ui.FAINT)
        )
        frame.row(line)

    # -- events ------------------------------------------------------------
    frame.rule("EVENTS")
    events = state["events"].tail(6)
    if not events:
        frame.row(ui.paint("waiting for activity…", ui.FAINT))
    for item in events:
        frame.row(state["events"].format(item, width - 4))

    return frame.close()


# ---------------------------------------------------------------------------
# Observation loop
# ---------------------------------------------------------------------------


class Observer:
    """Turns raw polling deltas into a human event feed."""

    def __init__(self) -> None:
        self.events = ui.EventLog(capacity=200)
        self.latency_hist: Deque[float] = deque(maxlen=40)
        self._prev: Dict[str, object] = {}
        self._attached = False

    def note(self, level: str, component: str, message: str) -> None:
        self.events.add(level, component, message)

    def update(self, state: dict) -> None:
        if not self._attached:
            self._attached = True
            self.note("info", "monitor", "attached to live telemetry")
        chain = state["chain"]
        if chain.get("ok"):
            self.latency_hist.append(float(chain.get("latency_ms", 0)))
            if self._prev.get("rpc") is False:
                self.note("ok", "rpc", f"reconnected · block {ui.human_int(chain['block'])}")
            self._prev["rpc"] = True
        else:
            if self._prev.get("rpc") is not False:
                self.note("error", "rpc", chain.get("error") or "connection lost")
            self._prev["rpc"] = False

        targets = state["targets"]
        if targets["updated_at"] != self._prev.get("targets_ts"):
            if self._prev.get("targets_ts"):
                self.note(
                    "ok",
                    "indexer",
                    f"targets refreshed · morpho {targets['morpho']} · dutch {targets['dutch']}"
                    f" · harvest {targets['harvest']}",
                )
            self._prev["targets_ts"] = targets["updated_at"]

        sniper = state["sniper"]
        if sniper:
            hits = int(sniper.get("verified", 0))
            if hits > int(self._prev.get("hits", 0) or 0):
                self.note("trade", "sniper", f"{hits} profitable simulation(s) verified")
            self._prev["hits"] = hits
            if sniper.get("error") and sniper.get("error") != self._prev.get("sniper_error"):
                self.note("warn", "sniper", str(sniper["error"])[:90])
            self._prev["sniper_error"] = sniper.get("error")

        db = state["db"]
        pending = len(db.get("pending", []))
        if pending != int(self._prev.get("pending", 0) or 0):
            self.note("info", "submitter", f"pending UserOps → {pending}")
            self._prev["pending"] = pending

        tripped = int(db.get("tripped_until") or 0)
        is_open = tripped > time.time()
        if is_open != bool(self._prev.get("circuit_open")):
            if is_open:
                self.note("error", "circuit", f"breaker OPEN · {db.get('last_error') or 'failures'}"[:90])
            else:
                self.note("ok", "circuit", "breaker closed")
            self._prev["circuit_open"] = is_open

        rows = db.get("rows", [])
        newest = int(rows[0]["created_at"]) if rows else 0
        if newest and newest != int(self._prev.get("newest_row", 0) or 0):
            row = rows[0]
            level = "ok" if str(row.get("status", "")).startswith("CONFIRMED") else "warn"
            self.note(
                level,
                "exec",
                f"{row.get('pipeline')} · {row.get('status')} · "
                f"sim {ui.usd(float(row.get('simulated_profit_usd') or 0), 4)}",
            )
            self._prev["newest_row"] = newest


async def collect(observer: Observer, session: aiohttp.ClientSession) -> dict:
    chain = await fetch_chain_state(session)
    state = {
        "chain": chain,
        "targets": read_targets(),
        "db": read_db(),
        "orch": _read_json(ORCH_STATUS),
        "sniper": _read_json(SNIPER_STATUS),
        "outbox": read_outbox_count(),
        "events": observer.events,
        "latency_hist": observer.latency_hist,
    }
    observer.update(state)
    return state


def snapshot_json(state: dict) -> dict:
    return {
        "chain": state["chain"],
        "targets": state["targets"],
        "orchestrator": state["orch"],
        "sniper": state["sniper"],
        "outbox": state["outbox"],
        "pnl": aggregate_pnl(state["db"].get("rows", [])),
        "pending_user_ops": state["db"].get("pending", []),
        "circuit": {
            "failures": state["db"].get("failures", 0),
            "tripped_until": state["db"].get("tripped_until", 0),
            "last_error": state["db"].get("last_error", ""),
        },
    }


async def run_live(interval: float, alternate: bool = True) -> None:
    ui.ensure_utf8()
    observer = Observer()
    screen = ui.Screen(alternate=alternate)
    async with aiohttp.ClientSession() as session:
        with screen:
            try:
                while True:
                    state = await collect(observer, session)
                    screen.draw(render(state))
                    await asyncio.sleep(max(0.25, interval))
            except KeyboardInterrupt:
                pass


async def run_once(as_json: bool = False) -> None:
    ui.ensure_utf8()
    observer = Observer()
    async with aiohttp.ClientSession() as session:
        state = await collect(observer, session)
    if as_json:
        print(json.dumps(snapshot_json(state), indent=2))
        return
    lines = render(state)
    if not sys.stdout.isatty():
        lines = [ui.to_ascii(line) for line in lines]
    print("\n".join(lines))


def print_telemetry_dashboard() -> None:
    """Backwards-compatible synchronous snapshot entrypoint."""
    asyncio.run(run_once())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator.monitor", description="Zerro-DEFI live telemetry")
    parser.add_argument("--once", action="store_true", help="print a single snapshot and exit")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a dashboard")
    parser.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds")
    parser.add_argument("--no-alternate", action="store_true", help="redraw in place without alt-screen")
    args = parser.parse_args(argv)

    try:
        if args.json or args.once:
            asyncio.run(run_once(as_json=args.json))
        else:
            asyncio.run(run_live(args.interval, alternate=not args.no_alternate))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
