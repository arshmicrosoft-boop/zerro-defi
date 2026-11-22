#!/usr/bin/env python3
"""Interactive Setup Wizard for Zerro-Defi.

Guides you through:
1. Choosing your $0-gas strategy (paymaster vs owned gas)
2. Configuring environment variables
3. Testing connections
4. Verifying ready for deployment

Usage:
    python scripts/setup.py [--env] [--test] [--all]
"""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
ROOT = _SCRIPTS.parent
for _p in (str(ROOT), str(ROOT / "py-orchestrator"), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

if str(ROOT / "py-orchestrator") not in sys.path:
    sys.path.insert(0, str(ROOT / "py-orchestrator"))

from console import (  # noqa: E402
    ACCENT,
    AMBER,
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

from dotenv import load_dotenv, set_key
import aiohttp

ENV_PATH = ROOT / ".env"

PAYMASTER_PROVIDERS = {
    "1": {"name": "Coinbase CDP (Free Tier)", "url": "https://portal.cdp.coinbase.com"},
    "2": {"name": "Pimlico (Free Tier)", "url": "https://dashboard.pimlico.io"},
    "3": {"name": "Custom Paymaster", "url": None},
    "4": {"name": "None (use own gas)", "url": None},
}


def choose(prompt: str, options: dict[str, dict]) -> str:
    """Present choices and return the key."""
    print(f"\n  {paint(prompt, SKY, bold=True)}")
    for key, opt in options.items():
        name = opt["name"]
        marker = "  [ ]" if key != "4" else "  [x]"
        print(f"    {marker} {key}. {name}")
    choice = input(f"\n  Select [{list(options.keys())}]: " ).strip()
    if choice not in options:
        print(f"  {paint('Invalid choice, defaulting to 4 (own gas)', AMBER)}")
        choice = "4"
    return choice


def set_env(key: str, value: str) -> None:
    """Set an environment variable in .env file."""
    set_key(str(ENV_PATH), key, value, quote_mode="never")


header_msg = """
╔══════════════════════════════════════════════════════════════════════════════╗
║  Zerro-Defi Interactive Setup Wizard                                         ║
║  $0-Capital / $0-Gas Autonomous MEV & Keeper System                          ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

greeting = f"""
{header_msg}

This wizard will guide you through configuring Zerro-Defi for:
 • Zero gas reserve via ERC-4337 Paymaster (recommended)
 • Or your own ETH gas reserve (slower but no external dependency)

Before we begin:
• Have your PRIVATE KEY ready (65 chars, 0x-prefixed)
• Have some ETH on Base Mainnet for UserOp signing (0.001-0.01 ETH)
  → NOTE: Signing is off-chain ECDSA, costs 0 gas, but bundler needs ETH

Let's determine your gas strategy...
"""


def main() -> int:
    ensure_utf8()
    args = sys.argv[1:]
    non_interactive = "--yes" in args or "-y" in args
    if non_interactive:
        print(greeting)

    # Load existing .env
    load_dotenv(ENV_PATH)

    # Check if already configured
    if ENV_PATH.exists() and not non_interactive:
        with open(ENV_PATH, "r") as f:
            if f.read().strip():
                try:
                    print(f"  {paint('✓', GREEN)} Existing .env found — press Enter to continue...")
                    input()
                except EOFError:
                    pass

    print("\n" + "=" * 76)

    # Step 1: Gas Strategy
    print("  " + paint("STEP 1: Gas Strategy", SKY, bold=True))
    print("  " + paint("How will you pay for gas on Base Mainnet?", MUTED))

    if non_interactive:
        # Non-interactive: detect from .env or default to paymaster
        api_key = os.getenv("PAYMASTER_BUNDLER_RPC", "")
        if api_key:
            gas_choice = "1"  # Coinbase CDP (assumed from key)
            print(f"\n  {paint('Detected:', SKY)} Paymaster API key in .env")
        else:
            gas_choice = "4"  # Own gas
            print(f"\n  {paint('Using:', SKY)} Your own gas reserve")
    else:
        gas_choice = choose("Select your gas payment method:", PAYMASTER_PROVIDERS)

    if gas_choice != "4":
        # Paymaster route
        opt = PAYMASTER_PROVIDERS[gas_choice]
        print(f"\n  {paint('Info:', SKY)} {opt['name']}")
        if opt["url"]:
            print(f"  Visit: {paint(opt['url'], ACCENT)}")
        api_key = ""
        if non_interactive:
            api_key = os.getenv("PAYMASTER_BUNDLER_RPC", "")
            if api_key:
                print(f"  Found paymaster key in .env")
                set_env("PAYMASTER_BUNDLER_RPC", api_key)
        else:
            api_key = input("\n  Enter your Paymaster/Bundler API key (or leave empty to skip): ").strip()
            if api_key:
                set_env("PAYMASTER_BUNDLER_RPC", api_key)

        # Confirm paymaster
        confirm = "y" if non_interactive else input("\n  Test paymaster connection? [Y/n]: ").strip().lower()
        if confirm in ("", "y", "yes"):
            if api_key:
                async def test_pm():
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            api_key,
                            json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            return (await resp.json()).get("result") == "0x2105"

                import asyncio
                try:
                    if asyncio.run(test_pm()):
                        print(f"  {paint('✓', GREEN)} Paymaster connection OK")
                    else:
                        print(f"  {paint('! Warning', AMBER)} Paymaster didn't return expected chain ID")
                except Exception as e:
                    print(f"  {paint('! Warning', AMBER)} Could not connect: {e}")

        print(f"  {paint('→', SKY)} With sponsored UserOps, you need 0 ETH in your EOA for gas")
        print(f"        (Signing is off-chain; bundler pays L2 gas; paymaster reimburses)")

    else:
        # Own gas route
        print(f"\n  {paint('!', AMBER)} Using your own gas reserve")
        print(f"  You MUST fund your EOA with ETH for UserOp signing and bundler fees")
        print(f"  Typical: 0.01-0.1 ETH on Base Mainnet (~$2-20)")
        set_env("PAYMASTER_BUNDLER_RPC", "")
        set_env("USE_OWN_GAS", "true")

    # Step 2: Wallet
    print("\n" + "=" * 76)
    print("  " + paint("STEP 2: Wallet Configuration", SKY, bold=True))
    print("  " + paint("Your operator EOA private key will never leave this machine.", MUTED))

    key = os.getenv("OPERATOR_PRIVATE_KEY") or os.getenv("OWNER_EOA_PRIVATE_KEY", "")
    if key and key != "0x" + "0" * 64:
        addr = key[2:12] + "…" + key[-6:]
        print(f"\n  {paint('✓', GREEN)} Found existing key ending in 0x{addr}")
        use_existing = True
        if not non_interactive:
            use_existing = input("  Use this key? [Y/n]: ").strip().lower()
            if use_existing == "":
                use_existing = True
    else:
        use_existing = False

    if non_interactive:
        key = key or ""
        if not key:
            print("  ⚠ No key in .env — run interactively or set OPERATOR_PRIVATE_KEY")
            return 1
        use_existing = True
    if not use_existing:
        key = input("\n  Paste your 0x-prefixed private key: ").strip()
        if not key.startswith("0x"):
            key = "0x" + key
        if len(key) != 66:
            print(f"  {paint('✗ Invalid key length', RED)} Key should be 66 chars (0x + 64 hex)")
            return 1

    set_env("OPERATOR_PRIVATE_KEY", key)
    print(f"  {paint('✓', GREEN)} Key saved to .env")

    # Derive account
    from eth_account import Account
    acct = Account.from_key(key)
    print(f"  Address: {paint(acct.address, SKY)}")

    # Step 3: Deploy Executor
    print("\n" + "=" * 76)
    print("  " + paint("STEP 3: Deploy Deterministic Executor", SKY, bold=True))
    print("  " + paint("The ZeroCapOmniExecutor uses CREATE2 for deterministic address:", MUTED))

    salt = "ZeroCapOmniExecutor:v2"  # Hardcoded in bootstrap_executor
    print(f"\n  {paint('Salt:', MUTED)} {salt}")
    print(f"  {paint('CREATE2 Predictor:', MUTED)} https://create2.whitfield.me")

    deploy_now = "y" if non_interactive else input("\n  Deploy executor now? (requires paymaster + funded wallet) [y/N]: ").strip().lower()
    if deploy_now == "y":
        import subprocess
        print(f"\n  {paint('→', SKY)} Running bootstrap_executor_zero_gas.py...")
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "bootstrap_executor_zero_gas.py")])
        if result.returncode == 0:
            print(f"\n  {paint('✓ Executor deployed', GREEN)}")
        else:
            print(f"\n  {paint('✗ Deployment failed', RED)} Check output above")

    # Step 4: Final Status
    print("\n" + "=" * 76)
    print("  " + paint("SETUP COMPLETE", ACCENT, bold=True))

    panel(
        "NEXT STEPS",
        [
            ("Test paymaster", paint("python scripts/test_paymaster_integration.py", INK)),
            ("Compile contracts", paint("python scripts/compile_and_verify.py", INK)),
            ("Run shadow cycle", paint("python scripts/run_live_shadow_cycle.py", INK)),
            ("Full launch", paint("python scripts/launch.py --live --yes", INK)),
            ("Telemetry", paint("python -m orchestrator.monitor --once", INK)),
        ],
        accent=ACCENT,
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)