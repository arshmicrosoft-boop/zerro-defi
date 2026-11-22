"""Provision the operator EOA, counterfactual SimpleAccount and executor address.

Generates a dedicated operator key when none exists, derives the deterministic
ERC-4337 account, compiles the executor and writes the CREATE2 address into
``.env``.  Safe to re-run: existing values are reused.

    python scripts/setup_operator_wallet.py
"""

from __future__ import annotations

import asyncio
import re
import secrets
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
    compute_arachnid_create2_address,
    get_counterfactual_sender,
)
from orchestrator.config import MORPHO_BLUE, UNI_ROUTER, WETH_BASE  # noqa: E402


def _upsert_env(text: str, key: str, value: str) -> str:
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, text, re.MULTILINE):
        return re.sub(pattern, f"{key}={value}", text, flags=re.MULTILINE)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"{key}={value}\n"


async def main() -> int:
    ensure_utf8()
    heading("Zerro-DEFI", "operator provisioning")
    print()

    env_path = ROOT_DIR / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

    key_match = re.search(r"^OPERATOR_PRIVATE_KEY=(.*)$", env_text, re.MULTILINE)
    current_key = key_match.group(1).strip() if key_match else ""

    if not current_key or current_key == "0x" + "0" * 64:
        current_key = "0x" + secrets.token_hex(32)
        status("ok", "operator", "generated a fresh dedicated EOA key")
    else:
        status("ok", "operator", "reusing OPERATOR_PRIVATE_KEY from .env")

    acct = Account.from_key(current_key)
    status("info", "eoa", paint(acct.address, SKY))

    artifact = compile_executor()
    creation_bytecode = bytes.fromhex(artifact["bytecode"][2:])

    async with aiohttp.ClientSession() as session:
        simple_account = await get_counterfactual_sender(session, acct.address, 0)
    status("ok", "smart account", paint(simple_account, SKY) + paint("  counterfactual", MUTED))

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

    env_text = _upsert_env(env_text, "OPERATOR_PRIVATE_KEY", current_key)
    env_text = _upsert_env(env_text, "EXECUTOR_ADDRESS", executor_addr)
    env_path.write_text(env_text, encoding="utf-8")
    status("ok", "env", f"{env_path.name} updated")

    panel(
        "PROVISIONED",
        [
            ("operator eoa", paint(acct.address, SKY)),
            ("smart account", paint(simple_account, SKY)),
            ("executor", paint(executor_addr, VIOLET)),
            ("salt", paint("ZeroCapOmniExecutor:v2", MUTED)),
            ("morpho blue", paint(MORPHO_BLUE, MUTED)),
            ("uniswap router", paint(UNI_ROUTER, MUTED)),
            ("weth", paint(WETH_BASE, MUTED)),
            ("next", paint("python scripts/launch.py --only compile,rust,launch", GREEN)),
        ],
        accent=ACCENT,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
