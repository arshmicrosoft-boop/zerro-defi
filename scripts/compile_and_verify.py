"""
Standalone Solidity compiler, artifact generator, unit-test runner and live
Base Mainnet fork verifier.

1. Compiles ZeroCapOmniExecutor.sol, ZeroCapOmniExecutor.t.sol and ZeroCapMainnetFork.t.sol.
2. Executes all 5 Solidity unit tests in ZeroCapOmniExecutorTest in the Cancun EVM.
3. Executes ZeroCapMainnetForkTest against LIVE Base Mainnet contracts.

    python scripts/compile_and_verify.py
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from console import (  # noqa: E402
    ACCENT,
    GREEN,
    INK,
    MUTED,
    ensure_utf8,
    heading,
    paint,
    panel,
    status,
)

ROOT_DIR = _SCRIPTS.parent
CONTRACTS_DIR = ROOT_DIR / "contracts"
OUT_DIR = CONTRACTS_DIR / "out"
SOLC_VERSION = "0.8.24"
BASE_RPC_URL = os.getenv("BASE_RPC_URLS", "https://base-rpc.publicnode.com").split(",")[0].strip()

UNIT_TESTS = [
    "testProfitableMorphoLiquidation (strict balance-delta dust check)",
    "testMorphoLiquidationRevertsIfBelowMinProfit",
    "testProfitableDutchAuctionWithSlippageGuards",
    "testProfitableHarvestBountyAndAllowlistGuard",
    "testRevertOnUnauthorizedCallbacks",
    "testRoutedMultiHopLiquidation",
    "testRoutedAerodromeLiquidation",
    "testFlashAction",
]

FORK_TESTS = [
    "testLiveMorphoFlashLoanAndUniswapV3Swap (real Morpho Blue + UniV3 + SimpleAccountFactory)",
]


def _sub_lines(names) -> None:
    for name in names:
        print(f"      {paint(name, MUTED)}")


def ensure_solc_binary() -> Path:
    tools_dir = ROOT_DIR / ".tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    is_win = platform.system().lower().startswith("win")
    bin_name = f"solc-{SOLC_VERSION}.exe" if is_win else f"solc-{SOLC_VERSION}"
    solc_path = tools_dir / bin_name

    if solc_path.exists() and solc_path.stat().st_size > 100_000:
        status("ok", "solc", f"v{SOLC_VERSION} cached")
        return solc_path

    url = (
        f"https://github.com/ethereum/solidity/releases/download/v{SOLC_VERSION}/solc-windows.exe"
        if is_win
        else f"https://github.com/ethereum/solidity/releases/download/v{SOLC_VERSION}/solc-static-linux"
    )
    status("info", "solc", f"downloading v{SOLC_VERSION}…")
    urllib.request.urlretrieve(url, solc_path)
    if not is_win:
        os.chmod(solc_path, 0o755)
    status("ok", "solc", f"v{SOLC_VERSION} installed")
    return solc_path


def _eth_call(payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        BASE_RPC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "zerro-defi-verifier/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_solidity_unit_tests_on_evm(test_runtime_bytecode: str, run_one_selector: str) -> None:
    test_addr = "0x0000000000000000000000000000000000007E57"
    started = time.perf_counter()
    passed = 0
    for idx, name in enumerate(UNIT_TESTS):
        body = _eth_call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [
                    {
                        "to": test_addr,
                        "data": "0x" + run_one_selector + f"{idx:064x}",
                        "gas": hex(80_000_000),
                    },
                    "latest",
                    {test_addr: {"code": test_runtime_bytecode}},
                ],
            }
        )
        if "error" in body:
            raise RuntimeError(f"unit test #{idx} ({name}) reverted in EVM: {body['error']}")
        if int(body.get("result", "0x0"), 16) != 1:
            raise RuntimeError(f"unit test #{idx} ({name}) did not pass")
        passed += 1

    if passed != len(UNIT_TESTS):
        raise RuntimeError(f"expected {len(UNIT_TESTS)} passing unit tests, got {passed}")

    status(
        "ok",
        "unit tests",
        f"{passed}/{len(UNIT_TESTS)} passed · Cancun EVM",
        time.perf_counter() - started,
    )
    _sub_lines(UNIT_TESTS)


def run_live_mainnet_fork_tests_on_evm(fork_runtime_bytecode: str, run_fork_selector: str) -> None:
    fork_test_addr = "0x000000000000000000000000000000000000F024"
    caller_addr = "0x000000000000000000000000000000000000CA11"
    started = time.perf_counter()
    body = _eth_call(
        {
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
    )
    if "error" in body:
        raise RuntimeError(f"live Base fork test reverted: {body['error']}")

    passed = int(body.get("result", "0x0"), 16)
    if passed != 1:
        raise RuntimeError(f"expected 1 passing fork test, got {passed}")

    status("ok", "fork test", "1/1 passed · live Base 8453", time.perf_counter() - started)
    _sub_lines(FORK_TESTS)


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

    started = time.perf_counter()
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

    creation_size = len(artifact["bytecode"]) // 2 - 1
    runtime_size = len(artifact["deployedBytecode"]) // 2 - 1
    status(
        "ok",
        "compile",
        f"ZeroCapOmniExecutor · {len(sources)} sources · "
        f"creation {creation_size:,} B · runtime {runtime_size:,} B",
        time.perf_counter() - started,
    )

    test_data = output["contracts"]["test/ZeroCapOmniExecutor.t.sol"]["ZeroCapOmniExecutorTest"]
    run_solidity_unit_tests_on_evm(
        "0x" + test_data["evm"]["deployedBytecode"]["object"],
        test_data["evm"]["methodIdentifiers"]["runOne(uint256)"],
    )

    if "test/ZeroCapMainnetFork.t.sol" in output["contracts"]:
        fork_data = output["contracts"]["test/ZeroCapMainnetFork.t.sol"]["ZeroCapMainnetForkTest"]
        run_live_mainnet_fork_tests_on_evm(
            "0x" + fork_data["evm"]["deployedBytecode"]["object"],
            fork_data["evm"]["methodIdentifiers"]["runAllForkTests()"],
        )

    return artifact


def main() -> int:
    ensure_utf8()
    heading("Zerro-DEFI", "contract build + verification")
    print()
    started = time.perf_counter()
    try:
        artifact = compile_executor()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        status("error", "failed", str(exc)[:220])
        return 1

    artifact_path = OUT_DIR / "ZeroCapOmniExecutor.json"
    panel(
        "ARTIFACT",
        [
            ("contract", paint("ZeroCapOmniExecutor", INK)),
            ("solc", paint(SOLC_VERSION, INK)),
            ("output", paint(str(artifact_path.relative_to(ROOT_DIR)), INK)),
            (
                "bytecode",
                paint(
                    f"{(len(artifact['bytecode']) // 2 - 1):,} B creation · "
                    f"{(len(artifact['deployedBytecode']) // 2 - 1):,} B runtime",
                    INK,
                ),
            ),
            ("elapsed", paint(f"{time.perf_counter() - started:.1f}s", GREEN)),
        ],
        accent=ACCENT,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
