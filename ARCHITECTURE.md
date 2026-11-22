# Architecture: 3-Strategy $0-Capital / $0-Gas Reserve Parallel Bot

## 1. Design goals

- **$0 trading capital**: strategies use Morpho Blue 0% flash loans or permissionless bounty calls.
- **$0 EOA gas reserve**: execution submitted as ERC-4337 UserOperations with sponsored paymaster.
- **Fail closed**: revert/sim failure means no submission.
- **Parallel evaluation**: Morpho, Dutch auction, and harvest pipelines run concurrently every block.
- **Separation of concerns**: indexing is slow/async; simulation is hot path; submission is policy-gated.

## 2. Component boundaries

```text
Base RPC/WebSocket
    │  newHeads / logs / eth_call
    ▼
┌─────────────────────┐      targets.json      ┌──────────────────────────┐
│ Python Indexer      │──────────────────────▶│ Rust Sniper Engine        │
│ - Morpho borrowers  │                        │ - per-block scheduler     │
│ - Beefy strategies  │                        │ - 3 async pipelines       │
│ - Auction targets   │◀──────────────────────│ - eth_call/estimateGas    │
│ - target TTL/state  │  health/metrics        │ - profit/risk gates       │
└─────────────────────┘                        └────────────┬─────────────┘
                                                            │ verified_outbox.json
                                                            ▼
                                               ┌──────────────────────────┐
                                               │ Python AA Submitter       │
                                               │ - build UserOp            │
                                               │ - pm_getPaymasterData     │
                                               │ - sign userOpHash         │
                                               │ - eth_sendUserOperation   │
                                               │ - receipt/nonce tracking  │
                                               └────────────┬─────────────┘
                                                            │ sponsored UserOp
                                                            ▼
                                               ┌──────────────────────────┐
                                               │ Base EntryPoint + Paymaster│
                                               └────────────┬─────────────┘
                                                            │ call
                                                            ▼
                                               ┌──────────────────────────┐
                                               │ ZeroCapOmniExecutor.sol   │
                                               │ - Morpho flash loan       │
                                               │ - liquidate / auction buy │
                                               │ - swap / repay / profit   │
                                               │ - harvest bounties        │
                                               └──────────────────────────┘
```

## 3. Strategy pipelines

### A. Morpho Blue micro-liquidations

- **Input**: candidate borrowers from Morpho Borrow/Repay/SupplyCollateral/WithdrawCollateral logs; market params; oracle/feed data.
- **Simulation**: build `executeMorphoLiquidation` calldata; `eth_estimateGas` from smart account or owner context; optionally `eth_call` with state override to assert owner profit token delta.
- **Execution**: flash loan loanToken from Morpho (0% fee), liquidate, swap seized collateral to loanToken via vetted router, repay exact assets, transfer surplus to owner.
- **Gate**: `profit > min_profit_usd`, gas estimate within cap, borrower still unhealthy at current block, route liquidity sufficient.

### B. Fee-burner Dutch auctions

- **Input**: auction contracts, epoch/payment token/reward tokens, price curve/read functions.
- **Simulation**: quote reward asset swap output off-chain and confirm on-chain with `eth_call`; only submit when net spread > threshold.
- **Execution**: flash loan payment token, `auction.buy(...)`, swap rewards to payment token, repay, transfer surplus.
- **Gate**: auction active, epoch valid, max payment amount bounded, slippage bounded, reward swap route allowlisted.

### C. Vault harvest bounties

- **Input**: Beefy/Yearn/other strategy addresses, reward token, call fee function, harvest selector.
- **Simulation**: read pending reward (`callReward`/equivalent), estimate gas, `eth_call` harvest from smart account.
- **Execution**: batch `harvest` through executor or direct smart account call; require bounty token balance increase.
- **Gate**: reward >= min bounty, harvest not reverted, strategy allowlisted, function signature verified per protocol.

## 4. ERC-4337 / $0-gas model

### Preferred: sponsored paymaster

- User EOA holds no ETH.
- Smart account is counterfactual; first UserOp may include `initCode`.
- Bundler+paymaster endpoint returns `paymasterAndData` / paymaster fields per EntryPoint version.
- Submission uses `eth_sendUserOperation`.

### Fallback: ERC-20 paymaster

- Gas deducted from profit token in `postOp`.
- Still $0 ETH reserve, but requires profit token balance/approval and paymaster support.
- Not suitable for strategies where profit token is unknown before execution unless swapped to accepted token.

### Latency budget

| Stage | Typical | Notes |
|---|---:|---|
| Target read | <1 ms | local JSON/memory |
| Simulation RPC | 20-150 ms | depends on RPC/provider |
| Paymaster sponsorship | 150-350 ms | kills oracle-race strategies |
| Bundler inclusion | 1 block+ | depends on fees/policy |

This is why only slow-decay/ignored-opportunity strategies survive.

## 5. Data contracts

### `targets.json` (Python -> Rust)

```json
{
  "chainId": 8453,
  "updatedAt": "2026-11-21T00:00:00Z",
  "owner": "0x...",
  "smartAccount": "0x...",
  "executor": "0x...",
  "morphoPositions": [
    {
      "marketId": "0x...",
      "borrower": "0x...",
      "loanToken": "0x...",
      "collateralToken": "0x...",
      "oracle": "0x...",
      "irm": "0x...",
      "lltv": "860000000000000000",
      "repayAssetsHint": "1000000",
      "uniFee": 500
    }
  ],
  "dutchAuctions": [],
  "harvestStrategies": [
    {"address": "0x...", "protocol": "beefy", "rewardToken": "0x4200...", "minRewardWei": "20000000000000"}
  ]
}
```

### `verified_outbox.json` (Rust -> Python)

```json
[
  {
    "id": "uuid",
    "strategy": "MORPHO_MICRO_LIQ",
    "target": "0xexecutor",
    "callData": "0x...",
    "estimatedGas": 420000,
    "minProfitWei": "100000",
    "deadlineBlock": 12345678
  }
]
```

## 6. Safety & risk controls

- **Allowlist**: routers, tokens, auction contracts, harvest strategies, markets.
- **Simulation-first**: no submission without successful simulation in same recent block window.
- **Deadline/TTL**: drop opportunities older than N blocks.
- **Nonce manager**: single in-flight UserOp per sender key or managed nonce queue.
- **Circuit breaker**: stop after consecutive simulation/submission failures.
- **Spend policy**: paymaster budget cap, per-op gas cap, daily profit/loss accounting.
- **Secrets**: load from env/secret manager; never commit keys.
- **Idempotency**: dedupe by `(strategy, target, block, calldata hash)`.

## 7. Observability

- Structured logs: JSON lines with `trace_id`, `strategy`, `block`, `target`, `sim_result`, `userop_hash`.
- Metrics: opportunities seen/simulated/submitted/included, profit token delta, paymaster rejections, RPC latency.
- Alerts: paymaster denial spike, RPC stale, nonce desync, negative profit invariant violation (should never happen on-chain).

## 8. Testing strategy

1. **Unit tests** - calldata encoding, profit math, target parsing, nonce/paymaster handling.
2. **Fork tests (Foundry)** - simulate Morpho liquidation, auction clear, harvest against Base fork at pinned block.
3. **Bundler dry-run** - build UserOp, request paymaster data, run `eth_estimateUserOperationGas`/simulation where supported.
4. **Testnet rehearsal** - deploy executor and smart account on Base Sepolia or equivalent AA-supported testnet; use sponsored paymaster sandbox.
5. **Mainnet shadow mode** - simulate only, no submission, compare predicted vs actual inclusion opportunities.

## 9. Known limitations / honest risks

- Paymaster sponsorship can be denied or rate-limited at any time.
- 250ms+ paymaster latency makes this unsuitable for latency races.
- Morpho micro-liquidations require accurate market params, oracle state, and swap route liquidity.
- Dutch auction interfaces vary by protocol; do not assume one ABI fits all.
- Beefy/Yearn harvest interfaces/reward tokens vary; verify per strategy.
- MEV/searcher competition can still capture opportunities.
- A buggy executor can revert harmlessly, but a bad approval/router integration can be catastrophic. Audit before use.
