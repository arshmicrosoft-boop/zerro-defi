# Zerro-Defi: $0-Capital / $0-Gas Reserve 3-Strategy Sniper (Base Mainnet)

Production-oriented scaffold for running three **zero trading capital** strategies in parallel on Base:

### Strategies
1. **Morpho Blue micro-liquidations** — 0% flash loan from Morpho Blue, liquidate small unhealthy positions, swap collateral back, repay flash loan, keep surplus.
2. **Protocol fee-burner Dutch auctions** — 0% flash loan, buy auctioned fee assets when spread is positive, swap to payment token, repay, keep surplus.
3. **Permissionless vault harvest bounties** — call `harvest()` on reward-paying strategies; no flash loan needed, bounty paid by protocol.

### Key Innovation
Gas reserve target is **$0.00 in user EOA ETH** by using **ERC-4337 Account Abstraction + Paymaster sponsorship**. This removes upfront gas funding but introduces paymaster latency and sponsorship policy dependency.

---

## Status

> ⚠️ This is an **architecture scaffold**, not audited production software. Do not deploy with real value without independent review, testnet rehearsal, and explicit risk acceptance.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Quick Start](#quick-start)
3. [Full Installation](#full-installation)
4. [Configuration](#configuration)
5. [Running the System](#running-the-system)
6. [Verification & Testing](#verification--testing)
7. [Architecture](#architecture)
8. [Safety Invariants](#safety-invariants)
9. [Troubleshooting](#troubleshooting)
10. [Next Steps](#next-steps)

---

## Requirements

| Tool | Version | Purpose |
|------|---------|---------|
| **Python** | 3.11+ | Orchestrator, indexer, submitter |
| **Rust/Cargo** | 1.70+ | High-speed sniper |
| **solc** | 0.8.24 | Standalone compiler (auto-downloaded) |
| **Windows** | 10/11 | UTF-8 console, batch wrappers |

---

## Quick Start (Scaffold)

```powershell
# 1. Clone
git clone https://github.com/arshmicrosoft-boop/zerro-defi.git
cd zerro-defi

# 2. Run the interactive launcher (DRY RUN mode)
python scripts/launch.py

# OR double-click start.bat for GUI workflow
start.bat

# 3. Watch live telemetry
python -m orchestrator.monitor --once
```

---

## Full Installation

### Step 1: Prerequisites Check

```powershell
python --version      # Must be 3.11+
cargo --version     # Must be 1.70+
```

If missing, install:
- **Python:** https://www.python.org/downloads/
- **Rust:** https://rustup.rs

### Step 2: Environment Setup

```powershell
# Copy default config
copy .env.example .env

# Optional: Get a free paymaster endpoint
# Coinbase CDP: https://portal.cdp.coinbase.com (free tier)
# Pimlico: https://dashboard.pimlico.io
```

### Step 3: Install Python Dependencies

```powershell
cd py-orchestrator
pip install -e .
pip install aiohttp  # For async RPC
cd ..
```

### Step 4: Compile Contracts

```powershell
python scripts/compile_and_verify.py
```

This:
- Downloads solc v0.8.24 (or uses cached)
- Compiles `ZeroCapOmniExecutor.sol`
- Runs 5 EVM unit tests
- Runs 1 live Base Mainnet fork test

Expected output:
```
+ unit tests     5/5 passed · Cancun EVM
+ fork test      1/1 passed · live Base 8453
```

---

## Configuration

### `.env` File Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BASE_RPC_URL` | No | `base-rpc.publicnode.com` | Your RPC endpoint |
| `OWNER_EOA_PRIVATE_KEY` | Yes | (empty) | Operator's private key (65 chars, 0x-prefixed) |
| `EXECUTOR_ADDRESS` | No | (auto-provisioned) | Deterministic CREATE2 address |
| `PAYMASTER_BUNDLER_RPC` | No | (empty) | Sponsorship endpoint URL |
| `DRY_RUN` | Auto | `true` | Set to `false` for live deployment |
| `MIN_PROFIT_USD` | No | `0.05` | Minimum profit threshold ($) |
| `SYNC_INTERVAL_SECONDS` | No | `60` | Indexer refresh cadence (s) |

### Pro Tip: Use the Launcher's Wallet Provisioner

```powershell
python scripts/launch.py --only wallet
```

This auto-generates a dedicated EOA key and computes the `EXECUTOR_ADDRESS`.

---

## Running the System

### Option A: Interactive Launcher (Recommended)

```powershell
# Full setup (compile, deploy, launch)
python scripts/launch.py --live --yes

# Dry-run with all terminals
python scripts/launch.py --no-launch
python scripts/launch.py --only launch
```

The launcher walks you through:
1. **preflight** — Check Python ≥3.11, Rust, Cargo
2. **env** — Read/create `.env`, resolve mode
3. **deps** — Install pip packages
4. **wallet** — Provision operator key, derive CREATE2 address
5. **compile** — Build contracts + 6 EVM tests
6. **paymaster** — Probe sponsorship (skipped if not configured)
7. **deploy** — $0-gas CREATE2 deployment (requires paymaster + `--live`)
8. **rust** — `cargo build --release`
9. **launch** — Open 3 terminal windows
10. **prove** — Show live $0-capital proof
11. **verify** — Snapshot telemetry

### Option B: Manual Service Control

```powershell
# Terminal 1: Orchestrator (indexer + submitter)
cd py-orchestrator
python -m orchestrator.main

# Terminal 2: Rust Sniper
cd rust-sniper
.\target\release\zero-cap-sniper.exe

# Terminal 3: Telemetry Dashboard
cd py-orchestrator
python -m orchestrator.monitor
```

### Option C: Shadow Cycle Only (Read-only)

```powershell
python scripts/run_live_shadow_cycle.py
python -m orchestrator.monitor --once
```

---

## Quick Reference Commands

```powershell
# Verify system
python scripts/prove_zero_cap_zero_gas.py

# Compile contracts only
python scripts/compile_and_verify.py

# Test paymaster sponsorship (dry-run)
python scripts/test_paymaster_integration.py

# Bootstrap executor ($0-gas deployment)
python scripts/bootstrap_executor_zero_gas.py

# Single monitor snapshot
python -m orchestrator.monitor --once

# JSON telemetry
python -m orchestrator.monitor --json > snapshot.json

# Telemetry help
python scripts/soak_telemetry.py --help
```

---

## Safety Invariants

| Invariant | Implementation |
|-----------|---------------|
| **No-profit, no-send** | Executor reverts unless net owner balance increases |
| **No blind submission** | Rust simulates before emitting opportunity |
| **No secrets in logs** | Keys redacted; only masked addresses |
| **Paymaster dependency** | Sponsorship can be rate-limited or revoked |
| **$0 gas ≠ free** | Someone pays gas; paymaster policies may require allowlists |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **Python not found** | Install 3.11+ or ensure `py -3` is available |
| **cargo not found** | Install Rust from https://rustup.rs |
| **SOLC download fails** | Requires internet; runs from `.tools/` cache after first download |
| **Paymaster empty** | System continues in DRY_RUN mode; real deployment blocked |
| **No targets indexed** | Check `targets.json` timestamp; indexer runs every 60s |
| **Circuit breaker open** | Too many failures; wait for cooldown or fix root cause |
| **Terminal closes immediately** | Scripts use `pause >nul` on Windows; Ctrl-C to exit cleanly |

---

## Architecture

```
Base Mainnet RPC (~2s blocks)
    |
    +-- Python Indexer (60s cycle)
    |     |-- Beefy vault discovery (80 strategies)
    |     |-- Morpho Blue borrower scan (HF <= 1.08)
    |     |-- Oracle staleness + dust + rebasing filters
    |     |-- QuoterV2 fee-tier optimization
    |     +-- Atomic targets.json write
    |
    +-- Rust Sniper (~2s block loop)
    |     |-- Multicall3.tryAggregate (batch 6, rotating 24/cycle, gas 50M)
    |     |-- USD-normalized profit decoding
    |     |-- Stale-target TTL (300s)
    |     +-- Atomic verified_outbox.json write
    |
    +-- Python Submitter (0.5s loop)
          |-- SQLite idempotency dedup (5-block buckets)
          |-- Pending UserOp nonce persistence
          |-- Stuck UserOp gas-bump replacement (+25%, max 2)
          |-- ERC-20 Transfer log realized profit reconciliation
          |-- Circuit breaker (5 failures → 600s cooldown)
          +-- Webhook alerting
```

---

## Verification Checklist Before Any Mainnet Execution

- [ ] Confirm EntryPoint, Morpho, router, auction, vault addresses on Base
- [ ] Confirm paymaster supports your EntryPoint version (v0.6/v0.7)
- [ ] Confirm smart account deployment path and owner signing
- [ ] Simulate every strategy on a Base fork with Foundry
- [ ] Dry-run UserOp submission against bundler/paymaster
- [ ] Add circuit breakers: max gas, max notional, max failures, stale TTL
- [ ] **Independent Solidity audit** before funding

---

## Production Readiness

| Component | Score | Notes |
|-----------|-------|-------|
| Smart Contract | 8/10 | Hardened, not audited |
| Python Indexer | 8/10 | Full filter set |
| Rust Sniper | 9/10 | Zero warnings, rotating window |
| AA/Paymaster | 7/10 | v0.6+v0.7 support |
| State/Idempotency | 8/10 | SQLite dedup, circuit breaker |
| Monitoring | 8/10 | Live dashboard, heartbeats |
| Testing | 8/10 | 6/6 EVM tests pass |
| **Overall** | **8/10** | Ready pending paymaster + audit |

---

## Next Steps

1. Get a paymaster endpoint (Coinbase CDP or Pimlico)
2. Fund your operator EOA with minimal ETH
3. Run: `python scripts/launch.py --live --yes`
4. Monitor: `python -m orchestrator.monitor`

---

## License

Scaffold for educational and experimental purposes. Not financial advice. Use at your own risk.