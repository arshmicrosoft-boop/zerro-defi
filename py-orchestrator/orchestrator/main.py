"""Zerro-DEFI orchestrator: indexer + ERC-4337 submitter with a live console."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Dict, List

from . import ui
from .config import (
    BASE_RPC_URLS,
    DRY_RUN,
    EXECUTOR_ADDRESS,
    MIN_PROFIT_USD,
    OPERATOR_PRIVATE_KEY,
    ROOT_DIR,
    TARGETS_FILE,
    VERIFIED_OUTBOX_FILE,
)
from .indexer import BaseStateIndexer
from .state_store import ExecutionStateStore
from .submitter import process_verified_outbox

STATUS_FILE = ROOT_DIR / "runtime" / "orchestrator_status.json"


class Config:
    def __init__(self) -> None:
        self.base_rpc_url = BASE_RPC_URLS[0]
        self.chain_id = 8453
        self.paymaster_bundler_rpc = os.getenv("PAYMASTER_BUNDLER_RPC", "")
        self.entrypoint = os.getenv("ENTRYPOINT_ADDRESS", "0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789")
        self.smart_account = os.getenv("SMART_ACCOUNT_ADDRESS", "")
        self.executor = EXECUTOR_ADDRESS
        self.owner_private_key = OPERATOR_PRIVATE_KEY
        self.targets_path = str(TARGETS_FILE)
        self.outbox_path = str(VERIFIED_OUTBOX_FILE)
        self.sync_interval_seconds = int(os.getenv("SYNC_INTERVAL_SECONDS", "60"))
        self.dry_run = DRY_RUN
        self.max_gas_per_op = int(os.getenv("MAX_GAS_PER_OP", "900000"))
        self.min_profit_wei = int(os.getenv("MIN_PROFIT_WEI", "15000000000000"))
        self.min_profit_usd = MIN_PROFIT_USD


def _outbox_count() -> int:
    try:
        if not VERIFIED_OUTBOX_FILE.exists():
            return 0
        raw = VERIFIED_OUTBOX_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return 0
        data = json.loads(raw)
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


def _write_status(status: dict) -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except Exception:
        pass


def _mode_badge(dry_run: bool) -> str:
    return ui.badge("DRY RUN", ui.AMBER, filled=True) if dry_run else ui.badge("LIVE", ui.RED, filled=True)


def render(status: dict, events: ui.EventLog, cfg: Config) -> List[str]:
    cols, _ = ui.term_size()
    width = max(78, min(cols - 1, 112))
    now = time.time()
    started = status.get("started_at", now)
    uptime = now - started

    frame = ui.Frame(
        width,
        title="ZERRO-DEFI  ·  ORCHESTRATOR",
        right=f"{ui.glyph('dot')} {'ARMED' if status.get('healthy') else 'SYNCING'}  {ui.now_clock()}",
        accent=ui.ACCENT,
    )

    frame.pair(
        _mode_badge(cfg.dry_run)
        + ui.paint("  BASE ", ui.MUTED)
        + ui.paint(str(cfg.chain_id), ui.INK, bold=True)
        + ui.paint("  ·  executor ", ui.MUTED)
        + ui.paint(ui.short_addr(cfg.executor, 8, 6), ui.SKY),
        ui.paint(f"uptime {ui.human_secs(uptime)}  ·  pid {status.get('pid', 0)}", ui.FAINT),
    )
    frame.pair(
        ui.paint("indexer ", ui.MUTED)
        + ui.paint(ui.short_addr(cfg.base_rpc_url.replace('https://', ''), 26, 0), ui.INK)
        + ui.paint("  ·  paymaster ", ui.MUTED)
        + ui.paint("configured" if cfg.paymaster_bundler_rpc else "missing", ui.GREEN if cfg.paymaster_bundler_rpc else ui.AMBER),
        ui.paint(f"min profit {ui.usd(cfg.min_profit_usd, 2)}", ui.FAINT),
    )

    frame.rule("SYNC")
    last_sync = status.get("last_sync_at", 0)
    elapsed = max(0.0, now - last_sync) if last_sync else 0.0
    interval = max(1, cfg.sync_interval_seconds)
    remaining = max(0.0, interval - elapsed) if last_sync else 0.0
    phase = min(1.0, elapsed / interval) if last_sync else 0.0
    state_color = ui.ACCENT if last_sync else ui.AMBER
    frame.pair(
        ui.paint("next refresh ", ui.MUTED)
        + ui.paint(f"{ui.human_secs(remaining)}", ui.INK, bold=True)
        + "  "
        + ui.meter(phase, 24, state_color),
        ui.paint(f"cycles {status.get('sync_count', 0)}  ·  last {status.get('last_sync_ms', 0)}ms", ui.FAINT),
    )

    sync = status.get("last_sync") or {}
    frame.pair(
        ui.paint("morpho ", ui.MUTED)
        + ui.paint(str(sync.get("morpho", 0)), ui.VIOLET, bold=True)
        + ui.paint("   dutch ", ui.MUTED)
        + ui.paint(str(sync.get("dutch", 0)), ui.SKY, bold=True)
        + ui.paint("   harvest ", ui.MUTED)
        + ui.paint(str(sync.get("harvest", 0)), ui.ACCENT, bold=True),
        ui.paint(f"tracked {sync.get('tracked', 0)} positions", ui.FAINT),
    )

    frame.rule("QUEUE")
    pending = int(status.get("pending", 0))
    outbox = int(status.get("outbox", 0))
    circuit_open = bool(status.get("circuit_open"))
    circuit_label = "OPEN" if circuit_open else "CLOSED"
    circuit_color = ui.RED if circuit_open else ui.GREEN
    frame.pair(
        ui.paint("outbox ", ui.MUTED)
        + ui.paint(str(outbox), ui.INK, bold=True)
        + ui.paint("   pending ", ui.MUTED)
        + ui.paint(str(pending), ui.AMBER if pending else ui.INK, bold=True)
        + ui.paint("   circuit ", ui.MUTED)
        + ui.paint(circuit_label, circuit_color, bold=True),
        ui.paint(f"failures {status.get('failures', 0)}", ui.FAINT),
    )
    if circuit_open and status.get("circuit_reason"):
        frame.row(ui.paint(ui.truncate(str(status["circuit_reason"]), width - 6), ui.RED))

    frame.rule("EVENTS")
    tail = events.tail(9)
    if not tail:
        frame.row(ui.paint("starting indexer…", ui.FAINT))
    for item in tail:
        frame.row(events.format(item, width - 4))

    if status.get("error"):
        frame.rule("LAST ERROR")
        frame.row(ui.paint(ui.truncate(str(status["error"]), width - 6), ui.RED))

    return frame.close()


async def _painter(screen: ui.Screen, status: dict, events: ui.EventLog, cfg: Config, stop: asyncio.Event) -> None:
    tick = 0
    while not stop.is_set():
        screen.draw(render(status, events, cfg))
        tick += 1
        await asyncio.sleep(0.25)


async def main() -> None:
    ui.ensure_utf8()
    cfg = Config()
    store = ExecutionStateStore(ROOT_DIR / "ops_state.db")

    events = ui.EventLog(capacity=300)
    ui.set_sink(lambda level, component, message: events.add(level, component, message))

    status: Dict[str, object] = {
        "updated_at": time.time(),
        "started_at": time.time(),
        "pid": os.getpid(),
        "mode": "dry-run" if cfg.dry_run else "live",
        "chain_id": cfg.chain_id,
        "executor": cfg.executor,
        "sync_count": 0,
        "last_sync_at": 0.0,
        "last_sync_ms": 0,
        "last_sync": {},
        "outbox": _outbox_count(),
        "pending": 0,
        "failures": 0,
        "circuit_open": False,
        "circuit_reason": "",
        "error": "",
        "healthy": False,
    }

    ui.log("orchestrator", f"boot · chain={cfg.chain_id} · mode={status['mode']}", "ok")
    ui.log("orchestrator", f"executor {ui.short_addr(cfg.executor, 10, 8)}", "info")
    if not cfg.paymaster_bundler_rpc:
        ui.log("orchestrator", "PAYMASTER_BUNDLER_RPC missing — submissions will simulate only", "warn")

    indexer = BaseStateIndexer()
    last_sync = 0.0
    stop = asyncio.Event()
    screen = ui.Screen()
    screen.enter()
    painter = asyncio.create_task(_painter(screen, status, events, cfg, stop))

    try:
        while True:
            now = time.time()

            if now - last_sync >= cfg.sync_interval_seconds:
                started = time.perf_counter()
                status["healthy"] = False
                try:
                    summary = await indexer.update_targets_file()
                    status["last_sync"] = summary or {}
                    status["error"] = ""
                    ui.log(
                        "indexer",
                        f"synced morpho={status['last_sync'].get('morpho', 0)} "
                        f"dutch={status['last_sync'].get('dutch', 0)} "
                        f"harvest={status['last_sync'].get('harvest', 0)}",
                        "ok",
                    )
                except Exception as exc:
                    status["error"] = str(exc)[:160]
                    ui.log("indexer", f"sync failed: {exc}", "error")
                status["last_sync_ms"] = int((time.perf_counter() - started) * 1000)
                status["sync_count"] = int(status["sync_count"]) + 1
                status["last_sync_at"] = time.time()
                last_sync = now
                status["healthy"] = True

            try:
                await process_verified_outbox()
            except Exception as exc:
                ui.log("submitter", f"loop error: {exc}", "error")
                status["error"] = str(exc)[:160]

            pending = store.get_pending_user_ops()
            is_open, reason = store.is_circuit_open()
            status["pending"] = len(pending)
            status["outbox"] = _outbox_count()
            status["circuit_open"] = is_open
            status["circuit_reason"] = reason
            status["updated_at"] = time.time()
            _write_status(status)

            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        painter.cancel()
        try:
            await painter
        except (asyncio.CancelledError, Exception):
            pass
        screen.leave()
        ui.clear_sink()
        ui.log("orchestrator", "shutdown complete", "warn")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
