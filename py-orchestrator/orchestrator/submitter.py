import json
import os
import time
import aiohttp
from eth_account import Account

from .aa import (
    build_sponsor_sign_and_send,
    extract_realized_profit_from_receipt,
    get_account_nonce,
    wait_for_user_op_receipt,
)
from . import ui
from .config import OPERATOR_PRIVATE_KEY, ROOT_DIR, VERIFIED_OUTBOX_FILE
from .monitor import send_alert
from .state_store import ExecutionStateStore

STATE_STORE = ExecutionStateStore(ROOT_DIR / "ops_state.db")
STUCK_USER_OP_TIMEOUT_SEC = int(os.getenv("STUCK_USER_OP_TIMEOUT_SEC", "45"))
MAX_REPLACEMENT_BUMPS = int(os.getenv("MAX_REPLACEMENT_BUMPS", "2"))


async def reconcile_pending_user_ops(session: aiohttp.ClientSession) -> None:
    pending = STATE_STORE.get_pending_user_ops()
    if not pending:
        return
    acct_addr = (
        Account.from_key(OPERATOR_PRIVATE_KEY).address
        if OPERATOR_PRIVATE_KEY and OPERATOR_PRIVATE_KEY != "0x" + "0" * 64
        else ""
    )
    now = int(time.time())

    for item in pending:
        op_hash = item["user_op_hash"]
        confirmed, tx_hash, status = await wait_for_user_op_receipt(
            session, op_hash, timeout_sec=5
        )
        if confirmed and tx_hash:
            realized_raw = await extract_realized_profit_from_receipt(
                session, tx_hash, item["profit_token"], [acct_addr, item["sender"]]
            )
            realized_usd = (realized_raw / (10 ** item["profit_decimals"])) * item[
                "token_price_usd"
            ]
            STATE_STORE.record_success(
                item["idempotency_key"], op_hash, tx_hash, str(realized_raw), realized_usd
            )
            await send_alert(
                session,
                "USER_OP_CONFIRMED",
                f"Confirmed {op_hash} | Realized=${realized_usd:.4f}",
                {"tx_hash": tx_hash, "realized_usd": realized_usd},
            )
            ui.log(
                "reconciler",
                f"confirmed {op_hash[:16]}… · realized {ui.usd(realized_usd)} · tx {tx_hash[:12]}…",
                "ok",
            )
            continue

        if status != "RECEIPT_TIMEOUT":
            STATE_STORE.record_failure(item["idempotency_key"], status, op_hash)
            ui.log("reconciler", f"{op_hash[:16]}… failed: {status}", "error")
            continue

        pending_nonce = int(item["nonce"], 16)
        current_nonce = await get_account_nonce(session, item["sender"])
        if current_nonce > pending_nonce:
            STATE_STORE.clear_pending_user_op(op_hash)
            ui.log(
                "reconciler",
                f"nonce {current_nonce} > pending {pending_nonce} · cleared {op_hash[:16]}…",
                "warn",
            )
            continue

        age = now - item["submitted_at"]
        if age < STUCK_USER_OP_TIMEOUT_SEC:
            continue

        if item["bump_count"] >= MAX_REPLACEMENT_BUMPS:
            STATE_STORE.record_failure(
                item["idempotency_key"],
                f"EXPIRED_AFTER_{item['bump_count']}_BUMPS",
                op_hash,
            )
            ui.log(
                "reconciler",
                f"expired after {item['bump_count']} gas bumps · {op_hash[:16]}…",
                "error",
            )
            continue

        old_max_fee = int(item["max_fee_per_gas"], 16)
        old_prio_fee = int(item["max_priority_fee_per_gas"], 16)
        bumped_max_fee = (old_max_fee * 125) // 100
        bumped_prio_fee = (old_prio_fee * 125) // 100

        inner_hex = item["inner_calldata_hex"]
        if not inner_hex or inner_hex == "0x":
            STATE_STORE.clear_pending_user_op(op_hash)
            continue

        ui.log(
            "reconciler",
            f"replacing stuck {op_hash[:16]}… on nonce {pending_nonce} (+25% bump #{item['bump_count'] + 1})",
            "warn",
        )
        (
            new_op_hash,
            new_tx_hash,
            new_status,
            sender,
            nonce_hex,
            max_fee_hex,
            max_prio_hex,
        ) = await build_sponsor_sign_and_send(
            session,
            bytes.fromhex(inner_hex[2:]),
            min_profit_raw=int(item["min_profit_raw"]),
            forced_nonce=pending_nonce,
            min_max_fee_per_gas=bumped_max_fee,
            min_priority_fee_per_gas=bumped_prio_fee,
        )

        if new_status == "CONFIRMED" and new_tx_hash:
            STATE_STORE.clear_pending_user_op(op_hash)
            realized_raw = await extract_realized_profit_from_receipt(
                session, new_tx_hash, item["profit_token"], [acct_addr, sender]
            )
            realized_usd = (realized_raw / (10 ** item["profit_decimals"])) * item[
                "token_price_usd"
            ]
            STATE_STORE.record_success(
                item["idempotency_key"],
                new_op_hash or op_hash,
                new_tx_hash,
                str(realized_raw),
                realized_usd,
            )
        elif new_status == "RECEIPT_TIMEOUT" and new_op_hash:
            STATE_STORE.record_pending_user_op(
                new_op_hash,
                item["idempotency_key"],
                sender,
                nonce_hex,
                inner_hex,
                item["min_profit_raw"],
                max_fee_hex,
                max_prio_hex,
                item["bump_count"] + 1,
                item["profit_token"],
                item["profit_decimals"],
                item["token_price_usd"],
            )
        else:
            STATE_STORE.record_failure(
                item["idempotency_key"], f"REPLACEMENT_ABORTED: {new_status}", op_hash
            )


async def process_verified_outbox() -> None:
    if not VERIFIED_OUTBOX_FILE.exists():
        return

    is_open, reason = STATE_STORE.is_circuit_open()
    if is_open:
        ui.log("submitter", f"circuit breaker active · {reason}", "error")
        return

    try:
        raw = VERIFIED_OUTBOX_FILE.read_text()
        if not raw.strip():
            return
        opportunities = json.loads(raw)
    except Exception:
        return

    if not opportunities:
        return

    async with aiohttp.ClientSession() as session:
        await reconcile_pending_user_ops(session)
        if STATE_STORE.has_pending_user_ops():
            ui.log("submitter", "UserOp in flight · holding nonce order", "warn")
            return

        acct_addr = (
            Account.from_key(OPERATOR_PRIVATE_KEY).address
            if OPERATOR_PRIVATE_KEY and OPERATOR_PRIVATE_KEY != "0x" + "0" * 64
            else ""
        )

        for opp in opportunities:
            calldata_hex = opp.get("executor_calldata", "")
            pipeline = opp.get("pipeline", "Unknown")
            desc = opp.get("description", "")
            sim_profit_raw = opp.get("simulated_profit_raw", "0")
            sim_profit_usd = float(opp.get("simulated_profit_usd", 0.0))
            min_profit_raw = int(opp.get("min_profit_raw", "0"))
            profit_token = opp.get("profit_token", "")
            profit_decimals = int(opp.get("profit_decimals", 18))
            token_price_usd = float(opp.get("token_price_usd", 1.0))
            block_num = int(opp.get("block_number", 0))

            if not calldata_hex.startswith("0x"):
                continue

            idem_key = STATE_STORE.compute_idempotency_key(pipeline, calldata_hex, block_num)
            if STATE_STORE.is_duplicate(idem_key):
                continue

            STATE_STORE.record_attempt(
                idem_key,
                pipeline,
                desc,
                block_num,
                profit_token,
                str(sim_profit_raw),
                sim_profit_usd,
            )

            inner_bytes = bytes.fromhex(calldata_hex[2:])
            (
                op_hash,
                tx_hash,
                status,
                sender,
                nonce_hex,
                max_fee_hex,
                max_prio_hex,
            ) = await build_sponsor_sign_and_send(
                session, inner_bytes, min_profit_raw=min_profit_raw
            )

            if status in ("CONFIRMED", "DRY_RUN_SIMULATED", "SKIPPED_NO_OPERATOR_KEY"):
                realized_raw = int(sim_profit_raw)
                realized_usd = sim_profit_usd
                if status == "CONFIRMED" and tx_hash and profit_token:
                    onchain_raw = await extract_realized_profit_from_receipt(
                        session, tx_hash, profit_token, [acct_addr, sender]
                    )
                    if onchain_raw > 0:
                        realized_raw = onchain_raw
                        realized_usd = (realized_raw / (10**profit_decimals)) * token_price_usd
                    await send_alert(
                        session,
                        "TRADE_CONFIRMED",
                        f"{pipeline}: {desc} | Realized=${realized_usd:.4f}",
                        {"tx_hash": tx_hash, "user_op_hash": op_hash},
                    )

                STATE_STORE.record_success(
                    idem_key,
                    op_hash or "DRY_RUN",
                    tx_hash or "DRY_RUN",
                    str(realized_raw),
                    realized_usd,
                )
                ui.log(
                    "submitter",
                    f"{status} · {pipeline} · sim {ui.usd(sim_profit_usd)} "
                    f"→ realized {ui.usd(realized_usd)} · tx {(tx_hash or 'dry-run')[:12]}…",
                    "trade" if status == "CONFIRMED" else "ok",
                )
            elif status == "RECEIPT_TIMEOUT" and op_hash:
                STATE_STORE.record_pending_user_op(
                    op_hash,
                    idem_key,
                    sender,
                    nonce_hex,
                    calldata_hex,
                    str(min_profit_raw),
                    max_fee_hex,
                    max_prio_hex,
                    0,
                    profit_token,
                    profit_decimals,
                    token_price_usd,
                )
                ui.log("submitter", f"{op_hash[:16]}… still pending · queued for reconciliation", "warn")
                break
            else:
                STATE_STORE.record_failure(idem_key, status, op_hash)
                is_open, cb_reason = STATE_STORE.is_circuit_open()
                if is_open:
                    await send_alert(session, "CIRCUIT_BREAKER_TRIPPED", cb_reason)
                ui.log("submitter", f"failed · {pipeline} · {status}", "error")

    VERIFIED_OUTBOX_FILE.write_text("[]")
