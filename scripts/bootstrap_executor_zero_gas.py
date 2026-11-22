import asyncio
import json
import sys
from pathlib import Path
import aiohttp
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "py-orchestrator"))

from scripts.compile_and_verify import compile_executor
from orchestrator.aa import (
    build_sponsor_sign_and_send,
    compute_arachnid_create2_address,
    ensure_executor_deployed_zero_gas,
    get_counterfactual_sender,
)
from orchestrator.config import (
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


async def main() -> None:
    artifact = compile_executor()
    creation_bytecode = bytes.fromhex(artifact["bytecode"][2:])

    async with aiohttp.ClientSession() as session:
        if OPERATOR_PRIVATE_KEY and OPERATOR_PRIVATE_KEY != "0x" + "0" * 64:
            eoa = Account.from_key(OPERATOR_PRIVATE_KEY).address
            owner_addr = await get_counterfactual_sender(session, eoa, 0)
        else:
            raise RuntimeError("OPERATOR_PRIVATE_KEY not set. Run python scripts/setup_operator_wallet.py first.")

        # 1. Construct with [] so CREATE2 hash is 100% invariant
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
        print(f"[Bootstrap] Owner (SimpleAccount)      : {owner_addr}")
        print(f"[Bootstrap] Deterministic CREATE2 Addr : {predicted}")

        addr, status = await ensure_executor_deployed_zero_gas(session, full_init_code, salt)
        print(f"[Bootstrap] CREATE2 Deploy Status      : {status}")

        # 2. Sync allowlisted targets from targets.json via setAllowedTargets()
        allowlist_batch = []
        if TARGETS_FILE.exists():
            try:
                tdata = json.loads(TARGETS_FILE.read_text())
                for h in tdata.get("harvest_targets", [])[:80]:
                    allowlist_batch.append(to_checksum_address(h["strategy"]))
                for d in tdata.get("dutch_targets", []):
                    allowlist_batch.append(to_checksum_address(d["auction_contract"]))
            except Exception:
                pass

        if allowlist_batch:
            calldata = encode_set_allowed_targets(allowlist_batch, True)
            op_hash, tx_hash, sync_status, _, _, _, _ = await build_sponsor_sign_and_send(
                session, calldata, target_address=addr, min_profit_raw=0
            )
            print(
                f"[Bootstrap] Synced {len(allowlist_batch)} allowlisted targets -> {sync_status} (UserOp={op_hash}, tx={tx_hash})"
            )


if __name__ == "__main__":
    asyncio.run(main())
