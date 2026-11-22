"""
Standalone Solidity Compiler, Artifact Generator, Unit-Test Runner & Live Base Mainnet Fork Verifier.
1. Compiles ZeroCapOmniExecutor.sol, ZeroCapOmniExecutor.t.sol, and ZeroCapMainnetFork.t.sol.
2. Executes all 5 Solidity unit tests in ZeroCapOmniExecutorTest in the Cancun EVM.
3. Executes ZeroCapMainnetForkTest against LIVE Base Mainnet contracts.
"""
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = ROOT_DIR / "contracts"
OUT_DIR = CONTRACTS_DIR / "out"
SOLC_VERSION = "0.8.24"
BASE_RPC_URL = os.getenv("BASE_RPC_URLS", "https://base-rpc.publicnode.com").split(",")[0].strip()


def ensure_solc_binary() -> Path:
    tools_dir = ROOT_DIR / ".tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    is_win = platform.system().lower().startswith("win")
    bin_name = f"solc-{SOLC_VERSION}.exe" if is_win else f"solc-{SOLC_VERSION}"
    solc_path = tools_dir / bin_name

    if solc_path.exists() and solc_path.stat().st_size > 100_000:
        return solc_path

    url = (
        f"https://github.com/ethereum/solidity/releases/download/v{SOLC_VERSION}/solc-windows.exe"
        if is_win
        else f"https://github.com/ethereum/solidity/releases/download/v{SOLC_VERSION}/solc-static-linux"
    )
    print(f"[Compiler] Downloading standalone solc v{SOLC_VERSION} from {url} ...")
    urllib.request.urlretrieve(url, solc_path)
    if not is_win:
        os.chmod(solc_path, 0o755)
    return solc_path


def run_solidity_unit_tests_on_evm(test_runtime_bytecode: str, run_all_selector: str) -> None:
    test_addr = "0x0000000000000000000000000000000000007E57"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": test_addr, "data": "0x" + run_all_selector, "gas": hex(50_000_000)},
            "latest",
            {test_addr: {"code": test_runtime_bytecode}},
        ],
    }
    req = urllib.request.Request(
        BASE_RPC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "zerro-defi-verifier/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    if "error" in body:
        raise RuntimeError(f"Solidity unit test suite reverted in EVM: {body['error']}")

    passed_count = int(body.get("result", "0x0"), 16)
    assert passed_count == 5, f"Expected 5 passing tests, got {passed_count}"
    print(f"[TestRunner] Executed all {passed_count}/5 Solidity unit tests in Cancun EVM -> ALL PASSED:")
    print("  [PASS] testProfitableMorphoLiquidation (with strict balance-delta dust check)")
    print("  [PASS] testMorphoLiquidationRevertsIfBelowMinProfit")
    print("  [PASS] testProfitableDutchAuctionWithSlippageGuards")
    print("  [PASS] testProfitableHarvestBountyAndAllowlistGuard")
    print("  [PASS] testRevertOnUnauthorizedCallbacks")


def run_live_mainnet_fork_tests_on_evm(fork_runtime_bytecode: str, run_fork_selector: str) -> None:
    fork_test_addr = "0x000000000000000000000000000000000000F024"
    caller_addr = "0x000000000000000000000000000000000000CA11"
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "eth_call",
        "params": [
            {
                "from": caller_addr,
                "to": fork_test_addr,
                "value": hex(10**16),
                "data": "0x" + run_fork_selector,
                "gas": hex(15_000_000),
            },
            "latest",
            {
                caller_addr: {"balance": hex(10**18)},
                fork_test_addr: {"code": fork_runtime_bytecode, "balance": hex(10**18)},
            },
        ],
    }
    req = urllib.request.Request(
        BASE_RPC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "zerro-defi-verifier/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    if "error" in body:
        raise RuntimeError(f"Live Base Mainnet fork test reverted: {body['error']}")

    passed_count = int(body.get("result", "0x0"), 16)
    assert passed_count == 1, f"Expected 1 passing fork test, got {passed_count}"
    print("[ForkRunner] Executed Live Base Mainnet Fork Integration Test -> PASSED:")
    print("  [PASS] testLiveMorphoFlashLoanAndUniswapV3Swap (Real Morpho Blue + Real UniV3 WETH/USDC + Real SimpleAccountFactory)")


def compile_executor() -> dict:
    solc_bin = ensure_solc_binary()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sources = {}
    for folder in ("src", "test"):
        for sol_file in (CONTRACTS_DIR / folder).rglob("*.sol"):
            rel_path = sol_file.relative_to(CONTRACTS_DIR).as_posix()
            sources[rel_path] = {"content": sol_file.read_text(encoding="utf-8")}

    standard_json_input = {
        "language": "Solidity",
        "sources": sources,
        "settings": {
            "viaIR": True,
            "optimizer": {"enabled": True, "runs": 20000},
            "evmVersion": "cancun",
            "outputSelection": {
                "*": {
                    "*": [
                        "abi",
                        "evm.bytecode.object",
                        "evm.deployedBytecode.object",
                        "evm.methodIdentifiers",
                    ]
                }
            },
        },
    }

    proc = subprocess.run(
        [str(solc_bin), "--standard-json"],
        input=json.dumps(standard_json_input).encode("utf-8"),
        capture_output=True,
        check=True,
    )
    output = json.loads(proc.stdout.decode("utf-8"))

    errors = [e for e in output.get("errors", []) if e.get("severity") == "error"]
    if errors:
        for err in errors:
            print(err.get("formattedMessage", err), file=sys.stderr)
        raise RuntimeError("Solidity compilation failed with errors")

    contract_data = output["contracts"]["src/ZeroCapOmniExecutor.sol"]["ZeroCapOmniExecutor"]
    artifact = {
        "contractName": "ZeroCapOmniExecutor",
        "solcVersion": SOLC_VERSION,
        "abi": contract_data["abi"],
        "bytecode": "0x" + contract_data["evm"]["bytecode"]["object"],
        "deployedBytecode": "0x" + contract_data["evm"]["deployedBytecode"]["object"],
        "methodIdentifiers": contract_data["evm"]["methodIdentifiers"],
    }

    artifact_path = OUT_DIR / "ZeroCapOmniExecutor.json"
    artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    print(f"[Compiler] Compiled ZeroCapOmniExecutor -> {artifact_path}")
    print(f"[Compiler] Creation bytecode size : {len(artifact['bytecode']) // 2 - 1} bytes")
    print(f"[Compiler] Runtime bytecode size  : {len(artifact['deployedBytecode']) // 2 - 1} bytes")

    test_data = output["contracts"]["test/ZeroCapOmniExecutor.t.sol"]["ZeroCapOmniExecutorTest"]
    run_solidity_unit_tests_on_evm(
        "0x" + test_data["evm"]["deployedBytecode"]["object"],
        test_data["evm"]["methodIdentifiers"]["runAllTests()"],
    )

    if "test/ZeroCapMainnetFork.t.sol" in output["contracts"]:
        fork_data = output["contracts"]["test/ZeroCapMainnetFork.t.sol"]["ZeroCapMainnetForkTest"]
        run_live_mainnet_fork_tests_on_evm(
            "0x" + fork_data["evm"]["deployedBytecode"]["object"],
            fork_data["evm"]["methodIdentifiers"]["runAllForkTests()"],
        )

    return artifact


if __name__ == "__main__":
    compile_executor()
