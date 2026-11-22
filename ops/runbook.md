# Operations Runbook: $0-Capital / $0-Gas 3-Strategy Bot

## 0. Preflight (mandatory)

- [ ] Confirm chain: **Base mainnet (8453)**.
- [ ] Confirm contract addresses (Morpho Blue, EntryPoint, router, WETH, auctions, strategies).
- [ ] Confirm paymaster/bundler supports your EntryPoint version and Base.
- [ ] Confirm smart account owner key is stored securely; never in git.
- [ ] Run `python scripts/prove_zero_cap_zero_gas.py`.
- [ ] Run Foundry fork tests against pinned Base block.

## 1. Environment

```bash
cp .env.example .env
# Set: BASE_RPC_URL, PAYMASTER_BUNDLER_RPC, SMART_ACCOUNT_ADDRESS, EXECUTOR_ADDRESS, OWNER_EOA_PRIVATE_KEY
# Keep DRY_RUN=true until shadow validation passes.
```

## 2. Deploy executor (gas-sponsored path)

1. Compile Solidity: `cd contracts && forge build`.
2. Build deployment calldata for `ZeroCapOmniExecutor(owner, morpho, router, weth)`.
3. Deploy via ERC-4337 UserOp `initCode`/factory or deterministic deployment proxy call, sponsored by paymaster.
4. Verify deployed bytecode and owner on Basescan.

**Do not** deploy with unreviewed code. The executor can approve routers and move tokens.

## 3. Start components

### Terminal A - Python indexer/submitter
```bash
cd py-orchestrator
python -m venv .venv
.venv\Scripts\activate
pip install -e .
python -m orchestrator.main
```

### Terminal B - Rust sniper
```bash
cd rust-sniper
cargo run --release
```

## 4. Shadow mode

- `DRY_RUN=true` in Rust and Python.
- Rust logs verified opportunities but does not write submission outbox.
- Python builds UserOps and requests paymaster sponsorship/simulation but does not call `eth_sendUserOperation`.
- Compare simulated opportunities against actual on-chain outcomes.

## 5. Production gates

Enable live submission only when all are true:

- [ ] 24h+ shadow mode with no false-positive profit simulations.
- [ ] Paymaster sponsorship success rate acceptable.
- [ ] Nonce manager handles retries/reorgs.
- [ ] Circuit breaker tested (RPC down, paymaster 4xx, simulation revert spike).
- [ ] Max gas, max notional, per-strategy min profit configured.
- [ ] Alerting on submission failures and unexpected balance deltas.

## 6. Incident response

- **Paymaster denial spike**: auto-disable submission, keep simulation running, alert.
- **Bad simulation/RPC inconsistency**: halt pipeline, compare two RPC providers.
- **Unexpected executor behavior**: transfer ownership to kill-switch account or pause via upgrade path (if implemented).
- **Nonce stuck**: inspect EntryPoint nonce, replace with same-nonce UserOp only if policy allows.

## 7. Economics reality check

- $0 capital means no inventory risk, but opportunity quality is limited.
- $0 gas reserve depends on third-party paymaster credits/policies.
- Micro-liquidations/harvests may yield cents to small dollars; monitor net realized profit, not simulated.
- Add accounting: token decimals, USD oracle, gas sponsored value, realized profit.