# Zerro-Defi: $0-Capital / $0-Gas Reserve 3-Strategy Sniper (Base Mainnet)

Production-oriented scaffold for running three **zero trading capital** strategies in parallel on Base:

1. **Morpho Blue micro-liquidations** - 0% flash loan from Morpho Blue, liquidate small unhealthy positions, swap collateral back, repay flash loan, keep surplus.
2. **Protocol fee-burner Dutch auctions** - 0% flash loan, buy auctioned fee assets when spread is positive, swap to payment token, repay, keep surplus.
3. **Permissionless vault harvest bounties** - call `harvest()` on reward-paying strategies; no flash loan needed, bounty paid by protocol.

Gas reserve target is **$0.00 in user EOA ETH** by using **ERC-4337 Account Abstraction + Paymaster sponsorship**. This removes upfront gas funding but introduces paymaster latency and sponsorship-policy dependency.

## Status

This is an **architecture scaffold**, not audited production software. Do not deploy with real value without independent review, testnet rehearsal, and explicit risk acceptance.

## Why these 3 survive $0-gas paymaster latency

| Strategy | Capital | Gas reserve | Latency tolerance | Why viable |
|---|---:|---:|---|---|
| Morpho micro-liquidations | $0 (0% flash loan) | $0 (sponsored UserOp) | Medium (seconds) | Micro positions are often ignored by large bots. |
| Fee-burner Dutch auctions | $0 (0% flash loan) | $0 (sponsored UserOp) | High (blocks/minutes) | Auction price decays gradually; $0 gas lowers profit threshold. |
| Vault harvest bounties | $0 (function call) | $0 (sponsored UserOp) | High (blocks/minutes) | Small bounties become economical when gas is sponsored. |

Strategies killed by paymaster latency (200-350ms): perp/liquidation keepers racing oracle updates, scheduled rebase/sync atomic arbs.

## Repository layout

```text
.
├── README.md
├── ARCHITECTURE.md
├── .env.example
├── .gitignore
├── contracts/                  # Solidity atomic executor + interfaces + Foundry tests
│   ├── foundry.toml
│   ├── src/
│   │   ├── ZeroCapOmniExecutor.sol
│   │   └── interfaces/
│   └── test/
├── rust-sniper/                # Real-time block sniper / simulator
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs
│       ├── config.rs
│       ├── types.rs
│       ├── sim.rs
│       └── pipelines/
├── py-orchestrator/            # Indexing, target sync, AA/paymaster submission
│   ├── pyproject.toml
│   └── orchestrator/
│       ├── config.py
│       ├── indexer.py
│       ├── aa.py
│       ├── submitter.py
│       └── main.py
├── scripts/
│   └── prove_zero_cap_zero_gas.py
└── ops/
    └── runbook.md
```

## Quick start (scaffold)

1. **Verify live assumptions** (RPC, Morpho liquidity, paymaster activity):
   ```bash
   python scripts/prove_zero_cap_zero_gas.py
   ```
2. **Configure environment**:
   ```bash
   cp .env.example .env
   # fill PAYMASTER_BUNDLER_RPC, OWNER_EOA_PRIVATE_KEY (for signing UserOps), SMART_ACCOUNT, EXECUTOR
   ```
3. **Run Python indexer + submitter**:
   ```bash
   cd py-orchestrator
   python -m venv .venv && .venv\Scripts\activate
   pip install -e .
   python -m orchestrator.main
   ```
4. **Run Rust sniper**:
   ```bash
   cd rust-sniper
   cargo run --release
   ```

## Critical safety invariants

- **No-profit, no-send**: executor reverts unless net owner balance increases in the profit token.
- **No blind submission**: Rust must simulate (`eth_call`/`eth_estimateGas`) before emitting an opportunity.
- **No secrets in logs**: private keys, raw paymaster tokens, and signed UserOps must be redacted.
- **Paymaster is a dependency, not a guarantee**: sponsorship can be rate-limited, revoked, or policy-filtered.
- **$0 gas reserve != free**: someone pays gas; paymaster policies may require allowlists, spending caps, or ERC-20 post-op fees.

## Verification checklist before any mainnet execution

- [ ] Confirm EntryPoint, Morpho, router, auction, and vault addresses on Base.
- [ ] Confirm paymaster supports your EntryPoint version (v0.6/v0.7) and target contract.
- [ ] Confirm smart account deployment/initCode path and owner signing.
- [ ] Simulate every strategy on a Base fork with Foundry.
- [ ] Dry-run UserOp submission against bundler/paymaster without broadcasting.
- [ ] Add circuit breakers: max gas, max notional, max failures, stale target TTL.
- [ ] Audit Solidity executor before funding/authorizing.
