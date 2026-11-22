import asyncio
import re
import secrets
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
from orchestrator.aa import compute_arachnid_create2_address, get_counterfactual_sender
from orchestrator.config import MORPHO_BLUE, UNI_ROUTER, WETH_BASE


async def main() -> None:
    env_path = ROOT_DIR / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

    key_match = re.search(r"^OPERATOR_PRIVATE_KEY=(.*)$", env_text, re.MULTILINE)
    current_key = key_match.group(1).strip() if key_match else ""

    if not current_key or current_key == "0x" + "0" * 64:
        current_key = "0x" + secrets.token_hex(32)
        print("[Setup] Generated fresh dedicated Operator EOA Private Key.")
    else:
        print("[Setup] Using existing OPERATOR_PRIVATE_KEY from .env.")

    acct = Account.from_key(current_key)
    artifact = compile_executor()
    creation_bytecode = bytes.fromhex(artifact["bytecode"][2:])

    async with aiohttp.ClientSession() as session:
        simple_account = await get_counterfactual_sender(session, acct.address, 0)

    ctor_args = abi_encode(
        ["address", "address", "address", "address", "address[]"],
        [
            to_checksum_address(simple_account),
            to_checksum_address(MORPHO_BLUE),
            to_checksum_address(UNI_ROUTER),
            to_checksum_address(WETH_BASE),
            [],
        ],
    )
    salt = keccak(text="ZeroCapOmniExecutor:v2")
    executor_addr = compute_arachnid_create2_address(salt, creation_bytecode + ctor_args)

    def upsert_env(text: str, k: str, v: str) -> str:
        pattern = rf"^{k}=.*$"
        if re.search(pattern, text, re.MULTILINE):
            return re.sub(pattern, f"{k}={v}", text, flags=re.MULTILINE)
        return text.rstrip() + f"\n{k}={v}\n"

    env_text = upsert_env(env_text, "OPERATOR_PRIVATE_KEY", current_key)
    env_text = upsert_env(env_text, "EXECUTOR_ADDRESS", executor_addr)
    env_path.write_text(env_text, encoding="utf-8")

    print(f"[Setup] Operator EOA Address             : {acct.address}")
    print(f"[Setup] Counterfactual ERC-4337 Account  : {simple_account}")
    print(f"[Setup] Deterministic EXECUTOR_ADDRESS   : {executor_addr}")
    print(f"[Setup] Updated {env_path} successfully.")


if __name__ == "__main__":
    asyncio.run(main())
