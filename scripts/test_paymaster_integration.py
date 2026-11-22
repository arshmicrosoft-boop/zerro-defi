"""Probe the ERC-4337 paymaster/bundler path without spending anything.

Derives the counterfactual SimpleAccount, reports deployment + nonce state and,
when a bundler RPC is configured, asks it to sponsor and estimate a dummy
UserOp.  Read-only: nothing is signed or submitted.

    python scripts/test_paymaster_integration.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiohttp
from eth_account import Account

_SCRIPTS = Path(__file__).resolve().parent
ROOT_DIR = _SCRIPTS.parent
for _p in (str(ROOT_DIR), str(ROOT_DIR / "py-orchestrator"), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from console import (  # noqa: E402
    ACCENT,
    GREEN,
    MUTED,
    RED,
    SKY,
    ensure_utf8,
    heading,
    paint,
    panel,
    status,
)
from orchestrator.aa import (  # noqa: E402
    encode_simple_account_execute,
    encode_simple_account_factory_initcode,
    get_account_nonce,
    get_counterfactual_sender,
    is_account_deployed,
    sponsor_and_estimate_user_op,
)
from orchestrator.config import (  # noqa: E402
    BASE_RPC_URLS,
    BUNDLER_RPC_URL,
    ENTRYPOINT_ADDRESS,
    OPERATOR_PRIVATE_KEY,
    PAYMASTER_RPC_URL,
)


async def main() -> int:
    ensure_utf8()
    heading("Zerro-DEFI", "paymaster probe")
    print()

    if not OPERATOR_PRIVATE_KEY or OPERATOR_PRIVATE_KEY == "0x" + "0" * 64:
        acct = Account.create()
        status("warn", "operator", f"no key in .env · ephemeral {paint(acct.address, SKY)}")
    else:
        acct = Account.from_key(OPERATOR_PRIVATE_KEY)
        status("info", "operator", paint(acct.address, SKY))

    async with aiohttp.ClientSession() as session:
        sender = await get_counterfactual_sender(session, acct.address, 0)
        deployed = await is_account_deployed(session, sender)
        nonce = await get_account_nonce(session, sender)

        status("info", "smart account", paint(sender, SKY))
        status("ok" if deployed else "info", "deployed", "yes" if deployed else "no · first UserOp will deploy it")
        status("info", "entrypoint nonce", str(nonce))

        if not BUNDLER_RPC_URL or not PAYMASTER_RPC_URL:
            status("skip", "sponsorship", "BUNDLER_RPC_URL / PAYMASTER_RPC_URL empty in .env")
            panel(
                "PAYMASTER",
                [
                    ("status", paint("not configured", RED)),
                    ("account", paint(sender, SKY)),
                    ("entrypoint", paint(ENTRYPOINT_ADDRESS, MUTED)),
                    ("next", paint("set PAYMASTER_BUNDLER_RPC in .env, then re-run", GREEN)),
                ],
                accent=ACCENT,
            )
            return 0

        init_code = "0x" if deployed else encode_simple_account_factory_initcode(acct.address, 0)
        call_data = encode_simple_account_execute(sender, 0, b"")

        async with session.post(
            BASE_RPC_URLS[0],
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []},
        ) as resp:
            base_fee = int((await resp.json()).get("result", "0x5f5e100"), 16)

        prio = max(base_fee // 5, 1_000_000)
        user_op = {
            "sender": sender,
            "nonce": hex(nonce),
            "initCode": init_code,
            "callData": call_data,
            "callGasLimit": hex(150_000),
            "verificationGasLimit": hex(450_000 if not deployed else 150_000),
            "preVerificationGas": hex(65_000),
            "maxFeePerGas": hex(base_fee * 2 + prio),
            "maxPriorityFeePerGas": hex(prio),
            "paymasterAndData": "0x",
            "signature": "0x"
            + "fffffffffffffffffffffffffffffff0000000000000000000000000000000007aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1c",
        }

        ok, message = await sponsor_and_estimate_user_op(session, user_op)
        status("ok" if ok else "error", "sponsorship", str(message)[:180])

    panel(
        "PAYMASTER",
        [
            ("result", paint("sponsored · gas covered" if ok else "rejected", GREEN if ok else RED)),
            ("account", paint(sender, SKY)),
            ("entrypoint", paint(ENTRYPOINT_ADDRESS, MUTED)),
            ("message", paint(str(message)[:120], MUTED)),
        ],
        accent=ACCENT,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
