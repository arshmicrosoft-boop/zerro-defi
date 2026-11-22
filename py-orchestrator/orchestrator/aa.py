import asyncio
import time
from typing import Dict, List, Optional, Tuple
import aiohttp
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak, to_checksum_address

from .config import (
    BASE_CHAIN_ID,
    BASE_RPC_URLS,
    BUNDLER_RPC_URL,
    DRY_RUN,
    ENTRYPOINT_ADDRESS,
    EXECUTOR_ADDRESS,
    OPERATOR_PRIVATE_KEY,
    PAYMASTER_RPC_URL,
)

SIMPLE_ACCOUNT_FACTORY = "0x9406Cc6185a346906296840746125a0E44976454"
ARACHNID_CREATE2_PROXY = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
ENTRYPOINT_V07_ADDRESS = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def is_entrypoint_v07(entrypoint: str) -> bool:
    return entrypoint.lower() == ENTRYPOINT_V07_ADDRESS.lower()


def encode_simple_account_execute(target: str, value: int, inner_calldata: bytes) -> str:
    selector = keccak(text="execute(address,uint256,bytes)")[:4]
    encoded_args = abi_encode(
        ["address", "uint256", "bytes"],
        [to_checksum_address(target), value, inner_calldata],
    )
    return "0x" + (selector + encoded_args).hex()


def encode_simple_account_factory_initcode(owner_address: str, salt: int = 0) -> str:
    selector = keccak(text="createAccount(address,uint256)")[:4]
    encoded_args = abi_encode(
        ["address", "uint256"], [to_checksum_address(owner_address), salt]
    )
    return SIMPLE_ACCOUNT_FACTORY + (selector + encoded_args).hex()


async def get_counterfactual_sender(
    session: aiohttp.ClientSession, owner_address: str, salt: int = 0
) -> str:
    selector = keccak(text="getAddress(address,uint256)")[:4]
    calldata = "0x" + (
        selector + abi_encode(["address", "uint256"], [to_checksum_address(owner_address), salt])
    ).hex()
    async with session.post(
        BASE_RPC_URLS[0],
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": SIMPLE_ACCOUNT_FACTORY, "data": calldata}, "latest"],
        },
    ) as resp:
        res = (await resp.json()).get("result", "0x")
        return to_checksum_address("0x" + res[-40:])


async def is_account_deployed(session: aiohttp.ClientSession, address: str) -> bool:
    async with session.post(
        BASE_RPC_URLS[0],
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [address, "latest"]},
    ) as resp:
        code = (await resp.json()).get("result", "0x")
        return len(code) > 2


async def get_account_nonce(session: aiohttp.ClientSession, sender: str) -> int:
    selector = bytes.fromhex("35567e1a")
    calldata = "0x" + (selector + abi_encode(["address", "uint192"], [to_checksum_address(sender), 0])).hex()
    async with session.post(
        BASE_RPC_URLS[0],
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": ENTRYPOINT_ADDRESS, "data": calldata}, "latest"],
        },
    ) as resp:
        res = (await resp.json()).get("result", "0x0")
        return int(res, 16)


def compute_user_op_hash(user_op: Dict[str, str], entry_point: str, chain_id: int) -> bytes:
    if is_entrypoint_v07(entry_point):
        init_code_bytes = (
            bytes.fromhex(user_op["factory"][2:] + user_op.get("factoryData", "0x")[2:])
            if user_op.get("factory")
            else b""
        )
        account_gas_limits = (
            int(user_op["verificationGasLimit"], 16).to_bytes(16, "big")
            + int(user_op["callGasLimit"], 16).to_bytes(16, "big")
        )
        gas_fees = (
            int(user_op["maxPriorityFeePerGas"], 16).to_bytes(16, "big")
            + int(user_op["maxFeePerGas"], 16).to_bytes(16, "big")
        )
        pm_bytes = (
            bytes.fromhex(
                user_op["paymaster"][2:]
                + int(user_op.get("paymasterVerificationGasLimit", "0x0"), 16).to_bytes(16, "big").hex()
                + int(user_op.get("paymasterPostOpGasLimit", "0x0"), 16).to_bytes(16, "big").hex()
                + user_op.get("paymasterData", "0x")[2:]
            )
            if user_op.get("paymaster")
            else b""
        )
        packed = abi_encode(
            ["address", "uint256", "bytes32", "bytes32", "bytes32", "uint256", "bytes32", "bytes32"],
            [
                to_checksum_address(user_op["sender"]),
                int(user_op["nonce"], 16),
                keccak(init_code_bytes),
                keccak(bytes.fromhex(user_op["callData"][2:])),
                account_gas_limits,
                int(user_op["preVerificationGas"], 16),
                gas_fees,
                keccak(pm_bytes),
            ],
        )
    else:
        packed = abi_encode(
            [
                "address",
                "uint256",
                "bytes32",
                "bytes32",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "bytes32",
            ],
            [
                to_checksum_address(user_op["sender"]),
                int(user_op["nonce"], 16),
                keccak(bytes.fromhex(user_op["initCode"][2:])),
                keccak(bytes.fromhex(user_op["callData"][2:])),
                int(user_op["callGasLimit"], 16),
                int(user_op["verificationGasLimit"], 16),
                int(user_op["preVerificationGas"], 16),
                int(user_op["maxFeePerGas"], 16),
                int(user_op["maxPriorityFeePerGas"], 16),
                keccak(bytes.fromhex(user_op["paymasterAndData"][2:])),
            ],
        )
    return keccak(
        abi_encode(
            ["bytes32", "address", "uint256"],
            [keccak(packed), to_checksum_address(entry_point), chain_id],
        )
    )


async def revalidate_inner_call_at_latest(
    session: aiohttp.ClientSession,
    sender: str,
    target: str,
    inner_calldata: bytes,
) -> Tuple[bool, int, str]:
    async with session.post(
        BASE_RPC_URLS[0],
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {
                    "from": to_checksum_address(sender),
                    "to": to_checksum_address(target),
                    "data": "0x" + inner_calldata.hex(),
                },
                "latest",
            ],
        },
        timeout=aiohttp.ClientTimeout(total=6),
    ) as resp:
        data = await resp.json()
        if "error" in data:
            return (False, 0, str(data["error"]))
        res = data.get("result", "0x")
        if res and len(res) >= 66:
            profit_raw = int(res[:66], 16)
            return (True, profit_raw, "")
        return (True, 0, "")


async def sponsor_and_estimate_user_op(
    session: aiohttp.ClientSession, user_op: Dict[str, str]
) -> Tuple[bool, str]:
    async with session.post(
        PAYMASTER_RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "pm_getPaymasterStubData",
            "params": [user_op, ENTRYPOINT_ADDRESS, hex(BASE_CHAIN_ID), {}],
        },
    ) as stub_resp:
        stub_json = await stub_resp.json()

    if "result" in stub_json and stub_json["result"]:
        stub_res = stub_json["result"]
        if "paymasterAndData" in stub_res:
            user_op["paymasterAndData"] = stub_res["paymasterAndData"]
        if "paymaster" in stub_res:
            user_op["paymaster"] = stub_res["paymaster"]
            user_op["paymasterData"] = stub_res.get("paymasterData", "0x")

        async with session.post(
            BUNDLER_RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_estimateUserOperationGas",
                "params": [user_op, ENTRYPOINT_ADDRESS],
            },
        ) as est_resp:
            est_json = await est_resp.json()
            if "error" in est_json:
                return (False, f"Bundler gas estimation reverted: {est_json['error']}")
            est = est_json.get("result", {})
            for k in ("callGasLimit", "verificationGasLimit", "preVerificationGas"):
                if k in est:
                    user_op[k] = est[k]

        async with session.post(
            PAYMASTER_RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "pm_getPaymasterData",
                "params": [user_op, ENTRYPOINT_ADDRESS, hex(BASE_CHAIN_ID), {}],
            },
        ) as pm_resp:
            pm_json = await pm_resp.json()
            if "error" in pm_json:
                return (False, f"pm_getPaymasterData error: {pm_json['error']}")
            pm_res = pm_json.get("result", {})
            if "paymasterAndData" in pm_res:
                user_op["paymasterAndData"] = pm_res["paymasterAndData"]
            if "paymaster" in pm_res:
                user_op["paymaster"] = pm_res["paymaster"]
                user_op["paymasterData"] = pm_res.get("paymasterData", "0x")
            return (True, "ERC-7677")

    async with session.post(
        PAYMASTER_RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "pm_sponsorUserOperation",
            "params": [user_op, ENTRYPOINT_ADDRESS],
        },
    ) as sponsor_resp:
        sp_json = await sponsor_resp.json()
        if "error" in sp_json:
            return (False, f"Paymaster sponsorship refused: {sp_json['error']}")
        res = sp_json.get("result", {})
        if "paymasterAndData" in res:
            user_op["paymasterAndData"] = res["paymasterAndData"]
        for k in ("callGasLimit", "verificationGasLimit", "preVerificationGas"):
            if k in res:
                user_op[k] = res[k]

    async with session.post(
        BUNDLER_RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "eth_estimateUserOperationGas",
            "params": [user_op, ENTRYPOINT_ADDRESS],
        },
    ) as est2_resp:
        est2_json = await est2_resp.json()
        if "error" in est2_json:
            return (False, f"Bundler simulation gate reverted: {est2_json['error']}")
        return (True, "pm_sponsorUserOperation")


async def extract_realized_profit_from_receipt(
    session: aiohttp.ClientSession,
    tx_hash: str,
    profit_token: str,
    recipient_addresses: List[str],
) -> int:
    if not tx_hash or not profit_token:
        return 0
    recipients_low = {addr.lower() for addr in recipient_addresses if addr}
    async with session.post(
        BASE_RPC_URLS[0],
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]},
    ) as resp:
        res = (await resp.json()).get("result")
        if not res or not isinstance(res, dict):
            return 0
        total_transferred = 0
        for log in res.get("logs", []):
            if log.get("address", "").lower() != profit_token.lower():
                continue
            topics = log.get("topics", [])
            if len(topics) >= 3 and topics[0].lower() == ERC20_TRANSFER_TOPIC:
                to_addr = "0x" + topics[2][-40:].lower()
                if to_addr in recipients_low:
                    total_transferred += int(log.get("data", "0x0"), 16)
        return total_transferred


async def wait_for_user_op_receipt(
    session: aiohttp.ClientSession, op_hash: str, timeout_sec: int = 35
) -> Tuple[bool, Optional[str], str]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        async with session.post(
            BUNDLER_RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getUserOperationReceipt",
                "params": [op_hash],
            },
        ) as resp:
            data = await resp.json()
            rec = data.get("result")
            if rec:
                success = bool(rec.get("success", False))
                tx_hash = rec.get("receipt", {}).get("transactionHash")
                return (
                    success,
                    tx_hash,
                    "CONFIRMED" if success else f"REVERTED_ON_CHAIN: {rec.get('reason', '')}",
                )
        await asyncio.sleep(1.5)
    return (False, None, "RECEIPT_TIMEOUT")


async def build_sponsor_sign_and_send(
    session: aiohttp.ClientSession,
    inner_calldata: bytes,
    target_address: Optional[str] = None,
    min_profit_raw: int = 0,
    forced_nonce: Optional[int] = None,
    min_max_fee_per_gas: int = 0,
    min_priority_fee_per_gas: int = 0,
) -> Tuple[Optional[str], Optional[str], str, str, str, str, str]:
    if not OPERATOR_PRIVATE_KEY or OPERATOR_PRIVATE_KEY == "0x" + "0" * 64:
        return (None, None, "SKIPPED_NO_OPERATOR_KEY", "", "0x0", "0x0", "0x0")

    target = target_address or EXECUTOR_ADDRESS
    acct = Account.from_key(OPERATOR_PRIVATE_KEY)
    sender = await get_counterfactual_sender(session, acct.address, 0)
    deployed = await is_account_deployed(session, sender)
    nonce = forced_nonce if forced_nonce is not None else await get_account_nonce(session, sender)
    nonce_hex = hex(nonce)

    if target.lower() == EXECUTOR_ADDRESS.lower() and int(EXECUTOR_ADDRESS, 16) != 0:
        ok_sim, latest_profit, err_sim = await revalidate_inner_call_at_latest(
            session, sender, target, inner_calldata
        )
        if not ok_sim or latest_profit < min_profit_raw:
            return (
                None,
                None,
                f"SUBMIT_REVALIDATION_FAILED (profit={latest_profit}, min={min_profit_raw}, err={err_sim})",
                sender,
                nonce_hex,
                "0x0",
                "0x0",
            )

    init_code = "0x" if deployed else encode_simple_account_factory_initcode(acct.address, 0)
    call_data = encode_simple_account_execute(target, 0, inner_calldata)

    async with session.post(
        BASE_RPC_URLS[0],
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []},
    ) as resp:
        base_fee = int((await resp.json()).get("result", "0x5f5e100"), 16)

    max_priority_fee = max(base_fee // 5, 1_000_000, min_priority_fee_per_gas)
    max_fee = max(base_fee * 2 + max_priority_fee, min_max_fee_per_gas)
    max_fee_hex = hex(max_fee)
    max_prio_hex = hex(max_priority_fee)

    user_op: Dict[str, str] = {
        "sender": sender,
        "nonce": nonce_hex,
        "initCode": init_code,
        "callData": call_data,
        "callGasLimit": hex(550_000),
        "verificationGasLimit": hex(450_000 if not deployed else 150_000),
        "preVerificationGas": hex(65_000),
        "maxFeePerGas": max_fee_hex,
        "maxPriorityFeePerGas": max_prio_hex,
        "paymasterAndData": "0x",
        "signature": "0x"
        + "fffffffffffffffffffffffffffffff0000000000000000000000000000000007aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1c",
    }

    if DRY_RUN or not BUNDLER_RPC_URL or not PAYMASTER_RPC_URL:
        op_hash = "0x" + compute_user_op_hash(user_op, ENTRYPOINT_ADDRESS, BASE_CHAIN_ID).hex()
        return (op_hash, None, "DRY_RUN_SIMULATED", sender, nonce_hex, max_fee_hex, max_prio_hex)

    ok, sponsor_msg = await sponsor_and_estimate_user_op(session, user_op)
    if not ok:
        return (None, None, f"SPONSOR_OR_SIM_FAILED: {sponsor_msg}", sender, nonce_hex, max_fee_hex, max_prio_hex)

    op_hash_bytes = compute_user_op_hash(user_op, ENTRYPOINT_ADDRESS, BASE_CHAIN_ID)
    signed_msg = Account.sign_message(
        encode_defunct(primitive=op_hash_bytes), private_key=OPERATOR_PRIVATE_KEY
    )
    user_op["signature"] = "0x" + signed_msg.signature.hex()

    async with session.post(
        BUNDLER_RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_sendUserOperation",
            "params": [user_op, ENTRYPOINT_ADDRESS],
        },
    ) as send_resp:
        send_data = await send_resp.json()
        if "error" in send_data:
            return (
                None,
                None,
                f"BUNDLER_SEND_REJECTED: {send_data['error']}",
                sender,
                nonce_hex,
                max_fee_hex,
                max_prio_hex,
            )
        op_hash_hex = send_data.get("result")

    confirmed, tx_hash, receipt_status = await wait_for_user_op_receipt(session, op_hash_hex)
    return (op_hash_hex, tx_hash, receipt_status, sender, nonce_hex, max_fee_hex, max_prio_hex)


def compute_arachnid_create2_address(salt_bytes32: bytes, init_code: bytes) -> str:
    data = b"\xff" + bytes.fromhex(ARACHNID_CREATE2_PROXY[2:]) + salt_bytes32 + keccak(init_code)
    return to_checksum_address("0x" + keccak(data)[-20:].hex())


async def ensure_executor_deployed_zero_gas(
    session: aiohttp.ClientSession,
    executor_init_code: bytes,
    salt_bytes32: bytes = b"\x00" * 32,
) -> Tuple[str, str]:
    predicted_addr = compute_arachnid_create2_address(salt_bytes32, executor_init_code)
    if await is_account_deployed(session, predicted_addr):
        return (predicted_addr, "ALREADY_DEPLOYED")

    deploy_payload = salt_bytes32 + executor_init_code
    _, tx_hash, status, _, _, _, _ = await build_sponsor_sign_and_send(
        session, deploy_payload, target_address=ARACHNID_CREATE2_PROXY, min_profit_raw=0
    )
    return (predicted_addr, f"{status} (tx={tx_hash})")
