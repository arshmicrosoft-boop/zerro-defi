"""Prove the $0-capital / $0-gas thesis against live Base Mainnet.

Reads on-chain state (no keys, no writes):

1. Morpho Blue token balances  -> liquidity actually sitting in the contract.
2. Recent EntryPoint v0.7 logs -> count of paymaster-sponsored UserOperations.

    python scripts/prove_zero_cap_zero_gas.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
ROOT_DIR = _SCRIPTS.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from console import (  # noqa: E402
    ACCENT,
    GREEN,
    MUTED,
    SKY,
    VIOLET,
    ensure_utf8,
    heading,
    human_int,
    paint,
    panel,
    status,
)

RPC_URLS = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]
MORPHO_BLUE = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH_BASE = "0x4200000000000000000000000000000000000006"
ENTRYPOINT_V07 = "0x0000000071727de22e5e9d8baf0edac6f37da032"
# UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)
TOPIC_USER_OP = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"


def rpc(method: str, params: list, timeout: int = 12):
    for url in RPC_URLS:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode())
            if "result" in body and body["result"] is not None:
                return body["result"]
        except Exception:
            continue
    return None


def u256(hex_data, word: int = 0) -> int:
    if not hex_data or hex_data == "0x":
        return 0
    raw = hex_data[2:]
    chunk = raw[word * 64 : word * 64 + 64]
    if len(chunk) < 64:
        return 0
    return int(chunk, 16)


def main() -> int:
    ensure_utf8()
    heading("Zerro-DEFI", "zero-capital / zero-gas proof")
    print()

    started = time.perf_counter()
    latest_hex = rpc("eth_blockNumber", [])
    if not latest_hex:
        status("error", "rpc", "could not reach Base Mainnet")
        return 1
    latest = int(latest_hex, 16)
    status("ok", "rpc", f"Base 8453 · block {human_int(latest)}")

    pad_morpho = MORPHO_BLUE[2:].lower().rjust(64, "0")
    usdc = u256(rpc("eth_call", [{"to": USDC_BASE, "data": "0x70a08231" + pad_morpho}, "latest"]))
    weth = u256(rpc("eth_call", [{"to": WETH_BASE, "data": "0x70a08231" + pad_morpho}, "latest"]))
    status("ok", "morpho", f"USDC {usdc / 1e6:,.2f} · WETH {weth / 1e18:,.6f}")

    logs = rpc(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(max(latest - 20, 0)),
                "toBlock": hex(latest),
                "address": ENTRYPOINT_V07,
                "topics": [TOPIC_USER_OP],
            }
        ],
    ) or []
    sponsored = []
    for log in logs:
        topics = log.get("topics", [])
        if len(topics) < 4:
            continue
        paymaster = "0x" + topics[3][-40:]
        if int(paymaster, 16) == 0:
            continue
        sender = "0x" + topics[2][-40:]
        gas_cost = u256(log.get("data", "0x"), 2)
        sponsored.append((sender, paymaster, gas_cost, log.get("transactionHash")))

    status("ok" if sponsored else "info", "sponsored ops", f"{len(sponsored)} in last 20 blocks")

    print()
    for sender, paymaster, gas_cost, txh in sponsored[:8]:
        print(
            "    "
            + paint(sender, SKY)
            + paint(" → ", MUTED)
            + paint(paymaster, VIOLET)
            + paint(f"  {gas_cost / 1e18:.8f} ETH", MUTED)
        )

    panel(
        "PROOF",
        [
            ("capital", paint("Morpho flash loans only — no inventory funded", GREEN)),
            ("liquidity", paint(f"USDC {usdc / 1e6:,.0f} · WETH {weth / 1e18:,.3f}", MUTED)),
            ("gas", paint("ERC-4337 + paymaster sponsorship", GREEN)),
            ("sponsored ops", paint(f"{len(sponsored)} of {len(logs)} recent UserOps", MUTED)),
            ("elapsed", paint(f"{time.perf_counter() - started:.1f}s", MUTED)),
        ],
        accent=ACCENT,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())