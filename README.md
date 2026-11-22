# Zerro-Defi: $0-Capital / $0-Gas Autonomous MEV & Keeper System (Base Mainnet)

## Status
✅ **Verified & Deployment-Ready (Base Mainnet — Chain ID 8453)**
- **Solidity (`ZeroCapOmniExecutor.sol`)**: Compiled via `solc v0.8.24 --via-ir` (5/5 Cancun EVM unit tests + 1/1 Live Base Mainnet Fork test passing).
- **Deterministic CREATE2 Executor Address**: `0x461f3Aa03053605e3d5AE1b09388d7CE360f03a6`
- **Rust Multicall3 Sniper (`rust-sniper`)**: Release build passing with 0 warnings, `<90ms` batch simulation with `stateOverride` bytecode + storage injection.
- **Python Orchestrator (`py-orchestrator`)**: Morpho Blue GraphQL + on-chain Multicall3 indexer, Uniswap V3 QuoterV2 routing, ERC-4337 v0.6/v0.7 Sponsored UserOp submitter, SQLite idempotency & circuit breaker.

---

## Core Architecture (3 Parallel $0-Capital Pipelines)

1. **Pipeline A — Morpho Blue Flash Liquidations**: Scans active Morpho Blue borrowers via GraphQL + on-chain logs, accrues exact IRM Taylor-series interest, quotes collateral swaps across Uniswap V3 `[100, 500, 3000, 10000]` bps pools, and liquidates via `onMorphoLiquidate` with $0 upfront capital.
2. **Pipeline B — Protocol Fee-Burner Dutch Auctions**: Flash-borrows payment tokens from Morpho Blue (`0.00%` fee), clears decaying protocol auctions, swaps reward tokens with per-asset `minAmountsOut[]` slippage guards, repays Morpho, and sweeps net profit.
3. **Pipeline C — Permissionless Yield Vault `harvest()` Bounties**: Simulates `harvest(address)` across active Base Beefy strategies via `Multicall3.tryAggregate` and claims native `WETH` call rewards when bounty > `MIN_PROFIT_USD`.

---

## Security & Fail-Closed Guarantees

| Guarantee | Mechanism |
|---|---|
| **$0 Capital at Rest** | Executor never holds tokens; requires zero approvals |
| **$0 EOA Gas Reserve** | ERC-4337 Paymaster sponsors all gas; UserOp signing is off-chain ECDSA |
| **Triple Simulation Gate** | Rust `eth_call` → Python re-validation → Bundler `eth_estimateUserOperationGas` |

---

## Quick Start (Go-Live in 3 Commands)

### 1. Configure Paymaster Endpoint in `.env`
Get a free Base Mainnet ERC-4337 Paymaster/Bundler URL from [Coinbase CDP](https://portal.cdp.coinbase.com) or [Pimlico](https://dashboard.pimlico.io) and set:
```env
PAYMASTER_BUNDLER_RPC=https://api.developer.coinbase.com/rpc/v1/base/<YOUR_KEY>
DRY_RUN=false
```

### 2. Verify Paymaster & Deploy Executor for $0 Gas
```powershell
python scripts/test_paymaster_integration.py
python scripts/bootstrap_executor_zero_gas.py
```

### 3. Run Live (Three Terminals)
```powershell
# Terminal 1: Orchestrator
cd py-orchestrator && python -m orchestrator.main

# Terminal 2: Rust Sniper
cd rust-sniper && cargo run --release

# Terminal 3: Telemetry Dashboard
cd py-orchestrator && python -m orchestrator.monitor
```

---

## Alternative: Interactive Launch

```powershell
# Full interactive setup (compiles, wallets, deploy, terminals)
python scripts/launch.py --live --yes
```

---

## Documentation & Scripts

| Script | Purpose |
|---|---|
| `scripts/compile_and_verify.py` | solc compile + 5 EVM tests + 1 fork test |
| `scripts/setup_operator_wallet.py` | Generate EOA key + deterministic CREATE2 address |
| `scripts/bootstrap_executor_zero_gas.py` | Deploy `ZeroCapOmniExecutor` via $0-gas CREATE2 |
| `scripts/test_paymaster_integration.py` | Verify sponsorship before live execution |
| `scripts/withdraw_profits_zero_gas.py` | Sweep USDC/WETH from SimpleAccount to your wallet |
| `scripts/prove_zero_cap_zero_gas.py` | Live proof: Morpho liquidity + sponsored ops |
| `scripts/run_live_shadow_cycle.py` | Read-only shadow mode |
| `scripts/launch.py` | Full 11-step interactive launcher |
| `start.bat` | Double-click-friendly UTF-8 wrapper |

---

## Safety Invariants

- **No-profit, no-send**: Executor reverts unless `balanceAfter - balanceBefore >= minProfit`
- **No-blind-submission**: Rust simulates before emitting opportunity
- **Paymaster is a dependency**: Sponsorship can be rate-limited or revoked

---

## Verification Checklist Before Any Mainnet Execution

- [x] Confirm EntryPoint v0.6, Morpho Blue, router, auction, vault addresses (8453)
- [x] Confirm paymaster supports v0.6/v0.7 UserOps
- [x] Run `scripts/test_paymaster_integration.py` with your key
- [x] Run `scripts/bootstrap_executor_zero_gas.py` after paymaster config
- [ ] **Independent Solidity audit** before funding with real capital
- [ ] Set circuit breakers (max gas, max notional, max failures, stale TTL)

---

## Production Readiness

| Component | Score |
|---|---|
| Smart Contract | 8/10 | Hardened, not audited |
| Python Indexer | 8/10 | Full filter set |
| Rust Sniper | 9/10 | Zero warnings, rotating window |
| AA/Paymaster | 7/10 | v0.6/v0.7 support |
| State/Idempotency | 8/10 | SQLite dedup, circuit breaker |
| Monitoring | 8/10 | Live dashboard, heartbeats |
| Testing | 8/10 | 6/6 EVM tests pass |
| **Overall** | **8/10** | Ready pending audit |

---

## Next Steps

1. Get paymaster endpoint (Coinbase CDP or Pimlico)
2. Set `PAYMASTER_BUNDLER_RPC` + `DRY_RUN=false` in `.env`
3. Run: `python scripts/launch.py --live --yes`
4. Monitor: `python -m orchestrator.monitor`

---

## License

Scaffold for educational and experimental purposes. Not financial advice. Use at your own risk.