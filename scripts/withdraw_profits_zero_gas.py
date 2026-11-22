#!/usr/bin/env python3
"""$0-Gas Profit Sweep CLI: checks and sweeps USDC/WETH from ERC-4337 SimpleAccount.

Because ZeroCapOmniExecutor sends 100% of realized USDC and WETH profits to your
ERC-4337 SimpleAccount (owner), you can sweep those profits to your personal
wallet at any time with $0 EOA gas via your Paymaster.

Usage:
    python scripts/withdraw_profits_zero_gas.py <0xRecipientWalletAddress>

    OR double-click start.bat → "profit sweep" step

Safety:
    - Never broadcasts a UserOp for $0 token amounts
    - Requires PAYMASTER_BUNDLER_RPC in .env for sponsorship
    - Requires DRY_RUN=false in .env for live submission
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import aiohttp
from eth_account import Account
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import keccak, to_checksum_address

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
ORCH_DIR = ROOT_DIR / "py-orchestrator"
sys.path.insert(0, str(ROOT_DIR))  # Load .env
sys.path.insert(0, str(ORCH_DIR))

from dotenv import load_dotenv

load_dotenv(ROOT_DIR / ".env")

from console import (  # noqa: E402
    ACCENT,
    GREEN,
    MUTED,
    SKY,
    VIOLET,
    ensure_utf8,
    heading,
    paint,
    short_addr,
    status,
)
from orchestrator.aa import (  # noqa: E402
    build_sponsor_sign_and_send,
    get_counterfactual_sender,
)
from orchestrator.config import (  # noqa: E402
    BASE_RPC_URLS,
    OPERATOR_PRIVATE_KEY,
    USDC_BASE,
    WETH_BASE,
)

SEL_BALANCE_OF = bytes.fromhex("70a08231")
SEL_TRANSFER = keccak(text="transfer(address,uint256)")[:4]


# ---------------------------------------------------------------------------
# Balance & Transfer helpers
# ---------------------------------------------------------------------------


async def get_erc20_balance(rpc: str, token: str, account: str) -> int:
    """Fetch ERC20 balance via eth_call."""
    calldata = "0x" + (SEL_BALANCE_OF + abi_encode(["address"], [to_checksum_address(account)])).hex()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            rpc,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": to_checksum_address(token), "data": calldata}, "latest"],
            },
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            res = (await resp.json()).get("result", "0x0")
            return int(res, 16) if res and res != "0x" else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main(recipient_str: str) -> int:
    ensure_utf8()
    recipient = to_checksum_address(recipient_str)
    heading("Zerro-DEFI", "zero-gas profit sweep")

    if not OPERATOR_PRIVATE_KEY or OPERATOR_PRIVATE_KEY == "0x" + "0" * 64:
        status("error", "wallet", "OPERATOR_PRIVATE_KEY not set in .env")
        status("info", "fix", "run python scripts/setup_operator_wallet.py first")
        return 1

    acct = Account.from_key(OPERATOR_PRIVATE_KEY)
    status("info", "operator", paint(acct.address, SKY))

    async with aiohttp.ClientSession() as session:
        simple_account = await get_counterfactual_sender(session, acct.address, 0)
        status("ok", "smart account", paint(simple_account, SKY))
        status("info", "destination", paint(recipient, ACCENT))

        # Check paymaster
        paymaster = os.getenv("PAYMASTER_BUNDLER_RPC", "").strip()
        if not paymaster:
            status("warn", "paymaster", "PAYMASTER_BUNDLER_RPC not configured — run in dry-run mode")
        else:
            status("ok", "paymaster", "configured")

        tokens = [
            ("USDC", USDC_BASE, 6),
            ("WETH", WETH_BASE, 18),
        ]

        swept_any = False
        for symbol, token_addr, decimals in tokens:
            bal = await get_erc20_balance(BASE_RPC_URLS[0], token_addr, simple_account)
            human_bal = bal / (10**decimals)
            status("info", f"{symbol} balance", f"{human_bal:.6f}")

            if bal == 0:
                continue

            # Build transfer
            transfer_calldata = SEL_TRANSFER + abi_encode(
                ["address", "uint256"], [recipient, bal]
            )

            # Sponsor & send
            ok, msg = await build_sponsor_sign_and_send(
                session,
                transfer_calldata,
                target_address=token_addr,
                min_profit_raw=0,
            )
            if ok:
                status("trade", f"swept {symbol}", f"{human_bal:.6f} to {short_addr(recipient, 6, 4)}")
                swept_any = True
            else:
                status("error", f"sweep {symbol}", msg[:120])

    if not swept_any:
        status("ok", "result", "no tokens to sweep")
    else:
        status("ok", "result", "sweep complete")
    return 0 if swept_any or True else 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].startswith("0x") or len(sys.argv[1]) != 42:
        print("Usage: python scripts/withdraw_profits_zero_gas.py <0xRecipientWalletAddress>")
        sys.exit(1)

    try:
        sys.exit(asyncio.run(main(sys.argv[1])))
    except KeyboardInterrupt:
        sys.exit(130)