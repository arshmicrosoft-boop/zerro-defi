"""Deploy ZeroCapOmniExecutor through the sponsored ERC-4337 path ($0 EOA gas).

Deploys the deterministic CREATE2 executor via the SimpleAccount + Paymaster
flow, then syncs the current ``targets.json`` allowlist through
``setAllowedTargets``.

    python scripts/bootstrap_executor_zero_gas.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

_SCRIPTS = Path(__file__).resolve().parent
ROOT_DIR = _SCRIPTS.parent
for _p in (str(ROOT_DIR), str(ROOT_DIR / "py-orchestrator"), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from console import (  # noqa: E402
    ACCENT,
    GREEN,
    MUTED,
    SKY,
    VIOLET,
    ensure_utf8,
    heading,
    paint,
    panel,
    status,
)
from scripts.compile_and_verify import compile_executor  # noqa: E402
from orchestrator.aa import (  # noqa: E402
    build_sponsor_sign_and_send,
    compute_arachnid_create2_address,
    ensure_executor_deployed_zero_gas,
    get_counterfactual_sender,
)
from orchestrator.config import (  # noqa: E402
    MORPHO_BLUE,
    OPERATOR_PRIVATE_KEY,
    TARGETS_FILE,
    UNI_ROUTER,
    WETH_BASE,
)


def encode_set_allowed_targets(targets: list[str], allowed: bool = True) -> bytes:
    selector = keccak(text="setAllowedTargets(address[],bool)")[:4]
    checksum_targets = [to_checksum_address(t) for t in targets]
    return selector + abi_encode(["address[]", "bool"], [checksum_targets, allowed])


async def main() -> int:
    ensure_utf8()
    heading("Zerro-DEFI", "zero-gas executor deployment")
    print()

    if not OPERATOR_PRIVATE_KEY or OPERATOR_PRIVATE_KEY == "0x" + "0" * 64:
        status("error", "operator", "OPERATOR_PRIVATE_KEY missing")
        status("info", "fix", "run python scripts/setup_operator_wallet.py first")
        return 1

    artifact = compile_executor()
    creation_bytecode = bytes.fromhex(artifact["bytecode"][2:])

    async with aiohttp.ClientSession() as session:
        eoa = Account.from_key(OPERATOR_PRIVATE_KEY).address
        owner_addr = await get_counterfactual_sender(session, eoa, 0)
        status("info", "owner", paint(owner_addr, SKY) + paint("  SimpleAccount", MUTED))

        # Construct with [] so the CREATE2 hash stays invariant.
        ctor_args = abi_encode(
            ["address", "address", "address", "address", "address[]"],
            [
                to_checksum_address(owner_addr),
                to_checksum_address(MORPHO_BLUE),
                to_checksum_address(UNI_ROUTER),
                to_checksum_address(WETH_BASE),
                [],
            ],
        )
        full_init_code = creation_bytecode + ctor_args
        salt = keccak(text="ZeroCapOmniExecutor:v2")
        predicted = compute_arachnid_create2_address(salt, full_init_code)
        status("info", "predicted", paint(predicted, VIOLET))

        addr, deploy_status = await ensure_executor_deployed_zero_gas(
            session, full_init_code, salt
        )
        level = "ok" if "deploy" in deploy_status.lower() else "info"
        status(level, "deploy", deploy_status)

        allowlist_batch: list[str] = []
        if TARGETS_FILE.exists():
            try:
                tdata = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
                for item in tdata.get("harvest_targets", [])[:80]:
                    allowlist_batch.append(to_checksum_address(item["strategy"]))
                for item in tdata.get("dutch_targets", []):
                    allowlist_batch.append(to_checksum_address(item["auction_contract"]))
            except Exception as exc:  # noqa: BLE001 - reported as a warning
                status("warn", "allowlist", f"targets.json unreadable · {exc}")

        sync_line = "no allowlist targets in targets.json"
        if allowlist_batch:
            calldata = encode_set_allowed_targets(allowlist_batch, True)
            op_hash, tx_hash, sync_status, _, _, _, _ = await build_sponsor_sign_and_send(
                session, calldata, target_address=addr, min_profit_raw=0
            )
            sync_line = (
                f"{len(allowlist_batch)} targets · {sync_status} · "
                f"userop {op_hash[:14]}… · tx {tx_hash[:14]}…"
            )
            status("ok", "allowlist", sync_line)

    panel(
        "EXECUTOR",
        [
            ("address", paint(addr, VIOLET)),
            ("owner", paint(owner_addr, SKY)),
            ("deploy", paint(deploy_status, GREEN)),
            ("allowlist", paint(sync_line, MUTED)),
            ("next", paint("python scripts/launch.py --live", GREEN)),
        ],
        accent=ACCENT,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
