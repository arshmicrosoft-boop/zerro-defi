import asyncio
import sys
from pathlib import Path
import aiohttp
from eth_account import Account

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "py-orchestrator"))

from orchestrator.aa import (
    encode_simple_account_execute,
    encode_simple_account_factory_initcode,
    get_account_nonce,
    get_counterfactual_sender,
    is_account_deployed,
    sponsor_and_estimate_user_op,
)
from orchestrator.config import (
    BASE_RPC_URLS,
    BUNDLER_RPC_URL,
    ENTRYPOINT_ADDRESS,
    OPERATOR_PRIVATE_KEY,
    PAYMASTER_RPC_URL,
)


async def main() -> None:
    if not OPERATOR_PRIVATE_KEY or OPERATOR_PRIVATE_KEY == "0x" + "0" * 64:
        acct = Account.create()
        print(f"[PaymasterTest] No OPERATOR_PRIVATE_KEY in .env; using ephemeral test key {acct.address}")
    else:
        acct = Account.from_key(OPERATOR_PRIVATE_KEY)
        print(f"[PaymasterTest] Using configured operator EOA: {acct.address}")

    async with aiohttp.ClientSession() as session:
        sender = await get_counterfactual_sender(session, acct.address, 0)
        deployed = await is_account_deployed(session, sender)
        nonce = await get_account_nonce(session, sender)
        print(f"[PaymasterTest] Counterfactual SimpleAccount : {sender}")
        print(f"[PaymasterTest] Deployed on Base Mainnet     : {deployed}")
        print(f"[PaymasterTest] Current EntryPoint Nonce     : {nonce}")

        if not BUNDLER_RPC_URL or not PAYMASTER_RPC_URL:
            print(
                "[PaymasterTest] SKIPPED live Paymaster RPC call because BUNDLER_RPC_URL / PAYMASTER_RPC_URL are empty in .env."
            )
            return

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

        ok, msg = await sponsor_and_estimate_user_op(session, user_op)
        print(f"[PaymasterTest] Sponsorship & Bundler Gas Gate Result: ok={ok} ({msg})")


if __name__ == "__main__":
    asyncio.run(main())
