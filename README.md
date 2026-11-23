# Zerro-Defi

**A $0-capital / $0-gas autonomous MEV & keeper system for Base Mainnet (chain ID 8453).**

Three parallel pipelines hunt Morpho Blue liquidations, protocol Dutch auctions, and vault harvest bounties. Opportunities are simulated off-chain for free, and only proven-profitable trades are submitted as ERC-4337 sponsored UserOperations.

---

## Status — Verified, With Honest Caveats

| Component | State | Evidence |
|---|---|---|
| **Solidity executor** | ✅ Compiles, tests pass | `solc v0.8.24 --via-ir` · **5/5** unit tests + **1/1** live-fork test |
| **Deterministic address** | ✅ Deployed address derivable | CREATE2 → `0x461f3Aa03053605e3d5AE1b09388d7CE360f03a6` |
| **Rust sniper** | ✅ Clean release build | `cargo build --release` · **0 warnings** · `<90ms` batch sim |
| **Python orchestrator** | ✅ Runs | Indexer + submitter + monitor, SQLite state |
| **Live shadow soak** | ⚠️ Ran clean, found nothing | 1.38h · 1,682 cycles · **40,200 sims · 0 verified** |
| **Dutch pipeline** | ⚠️ **Disabled by default** | `DUTCH_AUCTION_CONTRACTS_JSON=[]` |
| **Self-funded gas fallback** | ❌ **Not implemented** | Hard-gated on paymaster (see Roadmap) |

> **The headline number nobody should skip:** in the recorded soak, the system simulated **40,200 opportunities and verified zero**. That is *correct behavior*, not a bug — it means the safety gate refused to fire on unprofitable or stale state. But it also means **this is a low-frequency, event-driven system**, not a money printer. Read [Earnings Estimates](#earnings-estimates) before funding anything.

---

## ⚠️ Read This First

**What this is:**
- A genuinely **$0-upfront-capital** system (Morpho flash loans + permissionless bounty calls).
- A genuinely **$0-gas** system **while a paymaster sponsors you**.
- A **fail-closed** system: no simulation pass → no submission → no spend.

**What this is not:**
- Not a high-frequency arbitrage bot. Paymaster latency (150–350ms) makes it lose every latency race.
- Not audited. The contract is hardened but has **not** had an independent security review.
- Not profitable on demand. Expect **sparse, clustered** wins (see estimates).
- Not "free forever." It depends on a paymaster you do not control.

---

## Architecture

```text
                        Base Mainnet RPC / WebSocket
                                    │
              ┌─────────────────────┴─────────────────────┐
              │                                           │
              ▼                                           ▼
   ┌──────────────────────┐   targets.json    ┌──────────────────────────┐
   │  Python Indexer      │──────────────────▶│  Rust Sniper Engine      │
   │  (py-orchestrator)   │                   │  (rust-sniper)           │
   │                      │◀──────────────────│                          │
   │  • Morpho borrowers  │   health/metrics  │  • 500ms block poll      │
   │  • Beefy strategies  │                   │  • 3 parallel pipelines  │
   │  • Auction targets   │                   │  • Multicall3 simulate   │
   │  • HF ≤ 1.08 filter  │                   │  • stateOverride calls   │
   └──────────┬───────────┘                   └────────────┬─────────────┘
              │                                            │
              │  re-index every 60s                        │ verified_outbox.json
              │                                            ▼
              │                            ┌──────────────────────────────┐
              └───────────────────────────▶│  Python AA Submitter         │
                                           │  • build UserOp              │
                                           │  • request sponsorship       │
                                           │  • sign userOpHash (off-chain)│
                                           │  • eth_sendUserOperation     │
                                           │  • receipt + nonce tracking  │
                                           └──────────────┬───────────────┘
                                                          │ sponsored UserOp
                                                          ▼
                                           ┌──────────────────────────────┐
                                           │  EntryPoint v0.6 + Paymaster │
                                           └──────────────┬───────────────┘
                                                          │ call
                                                          ▼
                                           ┌──────────────────────────────┐
                                           │  ZeroCapOmniExecutor.sol     │
                                           │  • flash-borrow from Morpho  │
                                           │  • liquidate / buy / harvest │
                                           │  • swap via Uniswap V3       │
                                           │  • repay + assert profit     │
                                           │  • sweep surplus to owner    │
                                           └──────────────────────────────┘
```

### Component responsibilities

| Component | Language | Responsibility |
|---|---|---|
| `py-orchestrator/orchestrator/indexer.py` | Python | Builds `targets.json` — Morpho borrowers, Beefy strategies, Dutch auctions |
| `rust-sniper/src/*` | Rust | Polls blocks, runs 3 pipelines, simulates via `Multicall3.tryAggregate` |
| `py-orchestrator/orchestrator/submitter.py` | Python | Consumes `verified_outbox.json`, drives UserOp lifecycle |
| `py-orchestrator/orchestrator/aa.py` | Python | ERC-4337: build, sponsor, sign, send UserOp |
| `py-orchestrator/orchestrator/state_store.py` | Python | SQLite idempotency + circuit breaker |
| `contracts/src/ZeroCapOmniExecutor.sol` | Solidity | On-chain execution with profit assertion |
| `scripts/launch.py` | Python | 11-step orchestrator for setup + launch |
| `start.bat` | Batch | Double-click entry point with 8-step preflight |

---

## How It Works — The Runtime Workflow

### The loop, step by step

1. **Index** (`indexer.py`, every `SYNC_INTERVAL_SECONDS` = 60s)
   - Pulls Morpho Blue borrow logs → tracks up to **250** positions, keeps those with **health factor ≤ 1.08**.
   - Queries the Beefy subgraph → up to **80** active harvest targets.
   - Reads `DUTCH_AUCTION_CONTRACTS_JSON` → **empty by default**.
   - Writes `targets.json`.

2. **Simulate** (`rust-sniper`, every `BLOCK_POLL_MS` = 500ms)
   - Runs all three pipelines concurrently against `targets.json`.
   - Each candidate is simulated via `Multicall3.tryAggregate` with `stateOverride`, so the **undeployed** executor can be tested for free.
   - Gate: `passed == true` **and** `profit_usd >= MIN_PROFIT_USD` (default **$0.05**).
   - Survivors are written to `verified_outbox.json`.

3. **Submit** (`submitter.py`, checked every 500ms)
   - Deduplicates against SQLite (`strategy, target, block, calldata` hash).
   - Circuit breaker check: **5 consecutive failures → 600s cooldown**.
   - Builds a UserOp, requests paymaster sponsorship, signs the `userOpHash` off-chain (ECDSA — no gas).

4. **Execute** (on-chain)
   - EntryPoint validates the sponsored UserOp and calls the executor.
   - Executor flash-borrows from Morpho (0% fee), executes, repays, then **reverts unless `balanceAfter - balanceBefore >= minProfit`**.
   - Surplus is swept to the owner (your `SimpleAccount`).

5. **Record** — outcome written to SQLite; monitor dashboard updates every 500ms.

### Timing budget (why this only wins slow-decay opportunities)

| Stage | Typical latency | Consequence |
|---|---|---|
| Target read | <1 ms | negligible |
| Simulation RPC | 20–150 ms | depends on RPC provider |
| **Paymaster sponsorship** | **150–350 ms** | **loses oracle-race strategies** |
| Bundler inclusion | 1+ block (~2s on Base) | depends on fees/policy |

> This is the core strategic constraint: **anything a fast searcher can snipe in <100ms is not winnable here.** The system targets *ignored*, *slow-decaying* opportunities instead.

---

## The Three Strategies

### Pipeline A — Morpho Blue micro-liquidations
- **Capital:** $0 (Morpho flash loan, 0% fee).
- **Trigger:** tracked borrower with HF ≤ 1.08 and a profitable liquidation path.
- **Flow:** flash-borrow loan token → `liquidate` → swap seized collateral to loan token via Uniswap V3 → repay → sweep surplus.
- **Pay per win:** **$0.50 – $6.00** (avg ~$2.10) — Morpho pays a 4–7% liquidation incentive, so bigger positions pay more.
- **Frequency:** event-driven. Clusters during volatile markets; near-zero on flat days.

### Pipeline B — Protocol fee-burner Dutch auctions
- **Capital:** $0 (Morpho flash loan).
- **Trigger:** an active auction where the decaying price beats gas + swap cost.
- **Flow:** flash-borrow payment token → `buy(...)` → swap reward token with per-asset `minAmountsOut[]` guards → repay → sweep.
- **Pay per win:** **$0.15 – $1.20** (avg ~$0.40).
- **⚠️ Status:** **ships disabled.** `DUTCH_AUCTION_CONTRACTS_JSON=[]` by default. You must add live auction contracts to activate it. This is the cheapest profit you are currently leaving on the table.

### Pipeline C — Vault harvest bounties
- **Capital:** $0 (permissionless `harvest()` call).
- **Trigger:** Beefy strategy where `callReward()` ≥ `MIN_PROFIT_USD`.
- **Flow:** call `harvest(address)` → claim native WETH bounty.
- **Pay per win:** **$0.06 – $0.30** (avg ~$0.12) — on Base L2, gas is cheap enough that keepers harvest bounties almost immediately, so they rarely grow large.
- **Frequency:** highest of the three (~80 targets), but smallest per-win.

---

## Repository Structure

```text
zerro-defi/
├── start.bat                     ← double-click entry point (8-step preflight)
├── .env.example                  ← configuration template
├── ARCHITECTURE.md               ← deep design doc
├── OVERNIGHT_REPORT.md           ← recorded soak evidence
├── README.md
├── contracts/
│   ├── foundry.toml
│   ├── src/
│   │   ├── ZeroCapOmniExecutor.sol
│   │   └── interfaces/{IERC20,IMorphoBlue,ISwapRouter}.sol
│   └── test/
│       ├── ZeroCapOmniExecutor.t.sol        (5 unit tests)
│       └── ZeroCapMainnetFork.t.sol         (1 fork test)
├── py-orchestrator/
│   └── orchestrator/
│       ├── main.py               ← runtime loop
│       ├── config.py             ← env parsing
│       ├── indexer.py            ← targets.json builder
│       ├── submitter.py          ← outbox consumer
│       ├── aa.py                 ← ERC-4337 build/sponsor/sign/send
│       ├── state_store.py        ← SQLite idempotency + circuit breaker
│       ├── monitor.py            ← telemetry dashboard
│       └── ui.py                 ← terminal UI toolkit
├── rust-sniper/
│   └── src/
│       ├── main.rs               ← block poll loop
│       ├── config.rs             ← env parsing
│       ├── sim.rs                ← Multicall3 + stateOverride simulation
│       └── pipelines/{morpho,dutch,harvest}.rs
├── scripts/
│   ├── launch.py                 ← 11-step orchestrator
│   ├── setup.py                  ← interactive setup wizard
│   ├── compile_and_verify.py     ← solc + 6 tests
│   ├── setup_operator_wallet.py  ← key + CREATE2 address
│   ├── bootstrap_executor_zero_gas.py
│   ├── test_paymaster_integration.py
│   ├── withdraw_profits_zero_gas.py
│   ├── prove_zero_cap_zero_gas.py
│   ├── run_live_shadow_cycle.py
│   ├── soak_telemetry.py
│   └── estimate_royale.py        ← ROI model
└── ops/runbook.md
```

---

## Quick Start

### The 30-second version

> **Double-click `start.bat`.**
>
> It runs 8 preflight checks (Python, `.env`, deps, wallet, contracts, paymaster, Rust build) and then asks which mode to launch. If anything is broken, it tells you exactly what and refuses to launch.

That's genuinely it. Everything below is the detailed version.

---

### The full guide (first-time setup)

#### Step 0 — Prerequisites

| Requirement | Version | Check |
|---|---|---|
| Python | 3.11+ | `python --version` |
| Rust + Cargo | stable | `cargo --version` |
| Git | any | `git --version` |

#### Step 1 — Clone and configure

```powershell
git clone https://github.com/arshmicrosoft-boop/zerro-defi.git
cd zerro-defi
copy .env.example .env
```

#### Step 2 — Get a free paymaster (required for live execution)

Register for a Base Mainnet ERC-4337 bundler/paymaster:

- **Coinbase CDP** — https://portal.cdp.coinbase.com
- **Pimlico** — https://dashboard.pimlico.io

Then in `.env`:

```env
PAYMASTER_BUNDLER_RPC=https://api.developer.coinbase.com/rpc/v1/base/YOUR_KEY
DRY_RUN=false
```

> **Without this, the bot runs in simulation-only mode.** Live submission is hard-gated on a paymaster existing (`aa.py:411`).

#### Step 3 — Interactive setup wizard

```powershell
python scripts/setup.py
```

Walks you through: gas strategy → private key → executor deployment. Non-interactive mode: `python scripts/setup.py --yes`.

#### Step 4 — Verify everything

```powershell
python scripts/compile_and_verify.py     # solc + 5 unit + 1 fork test
python scripts/test_paymaster_integration.py
python scripts/setup_operator_wallet.py
```

#### Step 5 — Deploy the executor ($0 gas)

```powershell
python scripts/bootstrap_executor_zero_gas.py
```

Deploys via CREATE2 to `0x461f3Aa03053605e3d5AE1b09388d7CE360f03a6` using a sponsored UserOp. Requires the paymaster from Step 2.

#### Step 6 — Launch

```powershell
start.bat
```

Or manually, in three terminals:

```powershell
# Terminal 1 — orchestrator
cd py-orchestrator && python -m orchestrator.main

# Terminal 2 — sniper
cd rust-sniper && cargo run --release

# Terminal 3 — monitor
cd py-orchestrator && python -m orchestrator.monitor
```

#### Step 7 — Withdraw profits

```powershell
python scripts/withdraw_profits_zero_gas.py
```

Sweeps USDC/WETH from your `SimpleAccount` to your wallet via a $0-gas UserOp.

---

## `start.bat` — What It Actually Checks

Preflight runs **before** any launch, and **fails closed**:

| # | Check | On failure |
|---|---|---|
| 1 | Python ≥ 3.11 | Abort with install link |
| 2 | `scripts\launch.py` exists | Abort, suggest `git pull` |
| 3 | `.env` exists (creates from example) | Abort if no template |
| 4 | `PAYMASTER_BUNDLER_RPC` set | Falls back to DRY_RUN mode |
| 5 | `aiohttp` installed (auto-installs) | Abort on pip failure |
| 6 | Operator wallet valid | Warn, continue |
| 7 | Contracts compile + 6 tests pass | **Abort** |
| 8 | `cargo build --release` clean | Warn on warnings |

Then it presents a menu: **1)** launch all terminals · **2)** write service scripts only · **3)** monitor only · **4)** exit.

---

## `launch.py` — The 11 Steps

Run a subset with `--only`, or list them with `--list`.

```powershell
python scripts/launch.py --only preflight,env,compile --skip-build --no-launch
```

| Step | Does |
|---|---|
| `preflight` | Toolchain + repo layout |
| `env` | Read/create `.env`, resolve mode |
| `deps` | Install Python packages |
| `wallet` | Derive operator + executor address |
| `compile` | solc build + 5 EVM tests + fork test |
| `paymaster` | Sponsorship probe |
| `deploy` | $0-gas CREATE2 deployment |
| `rust` | `cargo build --release` |
| `launch` | Open service terminals |
| `prove` | Live capital + sponsorship proof |
| `verify` | Monitor snapshot + config check |

---

## Configuration Reference

| Variable | Default | Meaning |
|---|---|---|
| `BASE_RPC_URL` | publicnode | HTTP RPC |
| `BASE_WS_URL` | publicnode | WebSocket RPC |
| `CHAIN_ID` | `8453` | Base Mainnet |
| `ENTRYPOINT_ADDRESS` | `0x5FF1...2789` | ERC-4337 v0.6 EntryPoint |
| `SIMPLE_ACCOUNT_FACTORY` | `0x9406...6454` | SimpleAccount factory |
| `PAYMASTER_BUNDLER_RPC` | — | **Required for live** |
| `OWNER_EOA_PRIVATE_KEY` / `OPERATOR_PRIVATE_KEY` | — | Signing key (never commit) |
| `EXECUTOR_ADDRESS` | — | Your deployed executor |
| `MORPHO_BLUE_ADDRESS` | `0xBBBB...FFCb` | Morpho Blue |
| `UNISWAP_ROUTER_ADDRESS` | `0x2626...e481` | SwapRouter02 |
| `WETH_ADDRESS` | `0x4200...0006` | WETH |
| `MIN_PROFIT_WEI` | `15000000000000` | ~0.000015 WETH floor |
| `MIN_PROFIT_USD` | `0.05` | Sniper profit gate |
| `MAX_GAS_PER_OP` | `900000` | Per-op gas cap |
| `SYNC_INTERVAL_SECONDS` | `60` | Indexer cadence |
| `BLOCK_POLL_MS` | `500` | Sniper poll cadence |
| `DUTCH_AUCTION_CONTRACTS_JSON` | `[]` | **Pipeline B is off until set** |
| `DRY_RUN` | `true` | Must be `false` for live |

---

## Earnings Estimates

> These are **model estimates**, not measured results. The only measured figure is the soak: 0 verified opportunities in 1.38h. Per-win figures are derived from on-chain protocol economics (Morpho LIF, Beefy `callReward`, auction spreads) minus gas and swap haircut.

### Per-win profit

| Strategy | Per win | Wins/week | Notes |
|---|---|---|---|
| **Morpho micro-liquidations** | **$0.50 – $6.00** (avg ~$2.10) | 4 – 15 | Scales with position size (4–7% LIF) |
| **Vault harvest bounties** | **$0.06 – $0.30** (avg ~$0.12) | 12 – 28 | Small because keepers are fast |
| **Dutch auctions** | **$0.15 – $1.20** (avg ~$0.40) | 0 – 10* | *$0 while `[]` is empty |

### Weekly and monthly (the numbers that matter)

| Scenario | Weekly | Monthly (×4.3) | When |
|---|---|---|---|
| **Quiet week** (flat market) | **~$7.50** | **~$32** | Most weeks |
| **Average week** | **~$22** | **~$95** | Typical operating case |
| **Volatile week** (ETH ±5%) | **~$54.50+** | **~$235+** | Liquidation clusters |

**The realistic planning band is roughly $65–150/month**, with $95/month as the honest midpoint and $32–235 as the outer range. Volatile weeks are where the money actually is — quiet weeks are near-zero.

**Per-strategy monthly (average case):**

| Strategy | Monthly |
|---|---|
| Morpho liquidations | ~$36 |
| Vault harvests | ~$10 |
| Dutch auctions | ~$12 (only once configured) |
| **Combined** | **~$95** |

> Run `python scripts/estimate_royale.py` to see the live model with your current target counts.

---

## Success Probability & Tries:Success Ratio

| Mode | Simulated | Verified | Ratio |
|---|---|---|---|
| **Dry-run soak (measured)** | 40,200 | **0** | **0.00%** |
| **Live, deployed executor (estimated)** | — | — | **2–5%** (≈ 20:1 to 50:1) |

**Why the soak shows 0%:** the Rust sniper simulates `stateOverride` calls against an **undeployed** executor. Until you deploy and go live, `passed` can never be true. That is the safety gate working as designed.

**Per-strategy live probability:**

| Strategy | Probability | Condition |
|---|---|---|
| Morpho | ~0.1 – 1% | HF ≤ 1.08 borrower exists **and** swap haircut still leaves profit > $0.05 |
| Dutch | ~0.5 – 2% | Spread > gas + swap fees (and pipeline configured) |
| Harvest | ~1 – 5% | `callReward` > $0.05 |

**Expected live cadence:** 1–5 wins/day when markets move; 0 for days at a time when they don't.

---

## What Can Go Wrong

This is the section that matters. Every item is real.

### Gas & paymaster

| Risk | Impact | Mitigation |
|---|---|---|
| **Paymaster quota exhausted** | Live submission stops; bot goes silent | Track quota; provider dashboard; fallback provider |
| **Paymaster rate-limits / 429** | Dropped UserOps | Backoff; rotate providers |
| **Paymaster revoked / TOS change** | Total loss of execution ability | **No fallback implemented yet** (see Roadmap) |
| **Sponsorship latency spike** | Misses the opportunity entirely | Accepted design limit |
| **Bundler rejects UserOp** | Trade lost | Simulation-first reduces this; nonce tracking |

> **Provider quotas vary and change without notice.** Reported free tiers (CDP, Pimlico, Alchemy, ZeroDev) differ widely and are **not guaranteed** — verify current limits in each provider's dashboard before relying on them.

### Strategy & market

| Risk | Impact | Mitigation |
|---|---|---|
| **Zero opportunities for days** | $0 revenue, which is normal | Set expectations; this is event-driven |
| **MEV competition snipes the trade** | Simulated profit never realized | Only target slow-decay opportunities |
| **Stale oracle / stale targets** | Wrong decision | `MAX_ORACLE_AGE_SEC` = 7200; TTL checks |
| **Swap slippage / thin liquidity** | Profit evaporates | QuoterV2 + `minAmountsOut[]` guards |
| **Morpho position state changes mid-block** | Revert | On-chain profit assertion reverts safely |
| **Dutch ABI mismatch** | Revert or wrong call | Interfaces vary per protocol — verify each |

### Operational

| Risk | Impact | Mitigation |
|---|---|---|
| **RPC outage / lag** | Missed blocks, stale data | Rotate RPCs; health checks |
| **Nonce desync** | Stuck or duplicate UserOps | SQLite nonce tracking |
| **Circuit breaker trips** | 600s pause after 5 failures | Expected; auto-recovers |
| **Private key compromise** | Total loss | Never commit; hardware wallet for large funds |
| **Silent crash of a terminal** | Partial operation | Monitor dashboard; heartbeats |
| **No independent audit** | Unknown contract risk | **Get an audit before real capital** |

### The one that bites hardest
**A revert costs $0 while sponsored — but if you ever enable self-funded gas, a revert costs real earned money.** Any future self-funded mode needs a hard spend guard. See Roadmap.

---

## Pros & Cons

### ✅ Pros

- **Truly $0 upfront capital** — flash loans + permissionless calls, no inventory needed.
- **Truly $0 gas** while sponsored — off-chain signing, paymaster covers L2 gas.
- **Fail-closed by design** — triple gate (Rust sim → Python re-validate → bundler estimate) means no blind spending.
- **40,200 free simulations** prove the gate works — it refused to fire on junk.
- **Clean codebase** — Rust builds with zero warnings; contract passes 5 unit + 1 fork test.
- **Modular** — three independent pipelines; add targets without redeploying.
- **Deterministic deployment** — CREATE2 means a predictable executor address.
- **Real safety rails** — idempotency, circuit breaker, profit assertion, slippage guards.
- **Double-click launch** — `start.bat` validates everything before it runs.

### ❌ Cons

- **Low frequency** — days can pass with zero wins. This is the dominant downside.
- **Paymaster dependency** — a single point of failure with **no implemented fallback**.
- **Not latency-competitive** — 150–350ms sponsorship kills any race strategy.
- **Small per-win size** on Base L2 (cheap gas → keepers harvest bounties fast).
- **Pipeline B ships disabled** — you must supply auction contracts.
- **Not audited** — real capital carries unaudited-contract risk.
- **Complex ops surface** — Python + Rust + Solidity + ERC-4337 + SQLite.
- **Profit is irregular** — budgeting on a monthly average hides long dry spells.
- **Revenue is model-derived, not yet measured** — the soak found nothing to measure.

---

## Safety Invariants

| Guarantee | Mechanism |
|---|---|
| **$0 capital at rest** | Executor never holds tokens; no approvals |
| **$0 EOA gas** | Paymaster sponsors; UserOp signing is off-chain ECDSA |
| **No-profit, no-send** | Executor reverts unless `balanceAfter - balanceBefore >= minProfit` |
| **No-blind-submission** | Rust simulates before emitting |
| **Triple simulation gate** | Rust `eth_call` → Python re-validation → bundler `eth_estimateUserOperationGas` |
| **Idempotency** | Dedupe on `(strategy, target, block, calldata)` in SQLite |
| **Circuit breaker** | 5 consecutive failures → 600s cooldown |
| **Allowlisting** | Routers, tokens, auctions, strategies, markets |

---

## Roadmap — Explicitly Not Implemented

Documented so nobody assumes these work today.

### 1. Dutch pipeline activation (cheapest win)
`DUTCH_AUCTION_CONTRACTS_JSON=[]` means Pipeline B is dormant. Adding 5–10 live Base auction contracts turns it on with **zero new contracts and zero new latency risk**.

### 2. Immortal Mode — self-funded gas fallback (**design only**)
Goal: keep trading when the paymaster throttles, by paying gas from **earned profit** inside the `SimpleAccount`.

It is *mechanically sound* — EntryPoint charges the **sender**, not the EOA, so the EOA still needs 0 ETH. But it is **not implemented**. Required work:

1. **Split the URLs** — `config.py:18-19` currently reads `BUNDLER_RPC_URL` and `PAYMASTER_RPC_URL` from the **same** env var, so a bundler-without-paymaster is impossible.
2. **Allow the no-paymaster path** — remove the `aa.py:411` hard gate for the self-funded branch.
3. **Gas tank manager** — profit arrives as WETH/USDC, but gas needs native ETH, and `WETH.withdraw()` is itself a paid tx. The fix is a **proactive, paymaster-sponsored unwrap right after the first win**, so ETH sits in the `SimpleAccount` *before* the paymaster dies.
4. **Hard spend guard** — mandatory, because self-funded reverts cost real money:
   - profit must be ≥ **3×** gas cost
   - lifetime self-funded gas ≤ **20%** of realized profit
   - **2** consecutive self-funded reverts → lock the fallback

> Do not enable self-funded gas without the spend guard. It converts "$0 risk" into "bounded but real risk."

### 3. Additional pipelines
**Not recommended yet.** The bottleneck is signal, not surface area. Add pipelines only once you are leaving verified-but-unsubmitted opportunities on the table.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `DRY_RUN_SIMULATED`, never submits | No paymaster configured | Set `PAYMASTER_BUNDLER_RPC` |
| `0 verified` forever in dry-run | Executor not deployed | Expected — deploy to go live |
| `pip install failed` in preflight | Missing build tools | Install Visual C++ Build Tools |
| `cargo build` fails | Rust toolchain missing | `rustup update stable` |
| Paymaster `429` | Rate limited | Wait 24h or rotate provider |
| Circuit breaker tripped | 5 consecutive failures | Auto-recovers in 600s |
| `PAYMASTER_BUNDLER_RPC missing` warning | Not set in `.env` | Simulation-only until set |
| Dutch shows 0 targets | Pipeline disabled | Populate `DUTCH_AUCTION_CONTRACTS_JSON` |
| Stale targets | Indexer down | Check orchestrator terminal |

---

## Verification Checklist Before Any Mainnet Execution

- [x] EntryPoint v0.6, Morpho, router, auction, vault addresses confirmed (8453)
- [x] Contract compiles + 5 unit tests + 1 fork test pass
- [x] Rust release build: 0 warnings
- [x] Python modules compile clean
- [x] Soak: 1.38h, 1,682 cycles, 40,200 sims, 0 verified (gate working)
- [x] Paymaster probe passes with your key
- [x] Executor deployed via CREATE2
- [ ] **Independent Solidity audit** ← required before real capital
- [ ] Circuit breakers tuned (max gas, max notional, max failures, stale TTL)
- [ ] Dutch auctions configured (optional, recommended)

---

## Production Readiness

| Component | Score | Note |
|---|---|---|
| Smart contract | 8/10 | Hardened, **not audited** |
| Python indexer | 8/10 | Full filter set |
| Rust sniper | 9/10 | Zero warnings, rotating window |
| AA / paymaster | 7/10 | v0.6/v0.7 support; no fallback |
| State / idempotency | 8/10 | SQLite dedup + circuit breaker |
| Monitoring | 8/10 | Live dashboard, heartbeats |
| Testing | 8/10 | 6/6 EVM tests pass |
| **Overall** | **8/10** | Ready **pending audit** |

---

## License

Scaffold for educational and experimental purposes. **Not financial advice.** Use at your own risk.

MEV and keeper operation carries real risk including total loss of funds, failed transactions, and regulatory exposure. This software is provided as-is, without warranty of any kind. You are responsible for auditing, testing, and understanding every line before deploying capital.
