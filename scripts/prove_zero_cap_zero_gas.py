import json
import requests

RPC_URLS = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]
MORPHO_BLUE = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH_BASE = "0x4200000000000000000000000000000000000006"
ENTRYPOINT_V07 = "0x0000000071727de22e5e9d8baf0edac6f37da032"
# UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)
TOPIC_USER_OP = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"


def rpc(method, params):
    for url in RPC_URLS:
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=12)
            r.raise_for_status()
            payload = r.json()
            if "result" in payload and payload["result"] is not None:
                return payload["result"]
        except Exception:
            continue
    return None


def u256(hex_data, word=0):
    if not hex_data or hex_data == "0x":
        return 0
    raw = hex_data[2:]
    start = word * 64
    chunk = raw[start : start + 64]
    if len(chunk) < 64:
        return 0
    return int(chunk, 16)


def main():
    latest_hex = rpc("eth_blockNumber", [])
    if not latest_hex:
        raise SystemExit("Could not connect to Base RPC")
    latest = int(latest_hex, 16)
    print(f"Connected to Base Mainnet block {latest}\n")

    pad_morpho = MORPHO_BLUE[2:].lower().rjust(64, "0")
    usdc = rpc("eth_call", [{"to": USDC_BASE, "data": "0x70a08231" + pad_morpho}, "latest"])
    weth = rpc("eth_call", [{"to": WETH_BASE, "data": "0x70a08231" + pad_morpho}, "latest"])

    print("=== Morpho Blue 0%-fee flash-loan liquidity (contract token balances) ===")
    print(f"USDC: {u256(usdc) / 1e6:,.2f}")
    print(f"WETH: {u256(weth) / 1e18:,.6f}")
    print("Note: balance indicates liquidity present; flash loan availability depends on Morpho state and execution context.\n")

    logs = rpc(
        "eth_getLogs",
        [{"fromBlock": hex(max(latest - 20, 0)), "toBlock": hex(latest), "address": ENTRYPOINT_V07, "topics": [TOPIC_USER_OP]}],
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

    print("=== Recent ERC-4337 sponsored UserOperations (EntryPoint v0.7) ===")
    for sender, paymaster, gas_cost, txh in sponsored[:8]:
        print(f"sender={sender} paymaster={paymaster} actualGasCost={gas_cost/1e18:.8f} ETH tx={txh}")
    print(f"Total sponsored UserOps in last 20 blocks: {len(sponsored)}")
    print("\nInterpretation: paymaster covered gas; user EOA did not need ETH reserve for that UserOp.")


if __name__ == "__main__":
    main()
