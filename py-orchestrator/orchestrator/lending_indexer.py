"""Aave V3 / Moonwell liquidation discovery — SCAFFOLD ONLY.

This module is intentionally inert by default. It documents the shape of the
targets the on-chain ``ZeroCapOmniExecutor.executeFlashAction`` path expects,
but it does **not** implement protocol-specific adapters. Do not enable it
until all of the following are true:

1. The Aave V3 Pool / Moonwell Comptroller, oracle, and data-provider
   addresses are verified for Base Mainnet (chain 8453) and supplied via env.
2. A per-protocol ``liquidationCall`` / ``liquidateBorrow`` calldata builder
   exists, including the exact repay amount and collateral selection.
3. The target contracts are added to the executor allowlist
   (``setAllowedTargets``), because ``executeFlashAction`` is fail-closed.
4. The seized-collateral -> debt-asset swap is modelled with the same route
   selection (Uniswap single/multi-hop + Aerodrome) used by the Morpho indexer.

The generic 0%-fee flash-loan wrapper (``executeFlashAction``) is implemented
and unit-tested. The discovery + calldata layer is not. Wiring this in before
those pieces exist would only emit candidates that revert on-chain.
"""

import os
from typing import List

import aiohttp

from . import ui

# Addresses are deliberately empty by default. Populate only with values you
# have verified against Base Mainnet explorers / protocol docs.
AAVE_V3_POOL_BASE = os.getenv("AAVE_V3_POOL_BASE", "").strip()
MOONWELL_COMPTROLLER_BASE = os.getenv("MOONWELL_COMPTROLLER_BASE", "").strip()
ENABLE_LENDING_PIPELINE = os.getenv("ENABLE_LENDING_PIPELINE", "false").lower() in {
    "1",
    "true",
    "yes",
}


class LendingLiquidationScaffold:
    """Placeholder discovery pipeline for Aave V3 / Moonwell liquidations.

    ``discover_targets`` always returns ``[]``. It exists so the integration
    surface is explicit and testable, not so it can be switched on blindly.
    """

    def __init__(self) -> None:
        self.aave_pool = AAVE_V3_POOL_BASE
        self.moonwell_comptroller = MOONWELL_COMPTROLLER_BASE
        self.enabled = ENABLE_LENDING_PIPELINE

    async def discover_targets(self, session: aiohttp.ClientSession) -> List[dict]:
        if not self.enabled:
            return []

        missing = []
        if not self.aave_pool:
            missing.append("AAVE_V3_POOL_BASE")
        if not self.moonwell_comptroller:
            missing.append("MOONWELL_COMPTROLLER_BASE")

        if missing:
            ui.log(
                "lending",
                "lending pipeline enabled but missing env: " + ", ".join(missing),
                "warn",
            )
            return []

        ui.log(
            "lending",
            "lending pipeline enabled, but protocol adapters are not implemented; "
            "returning no targets (see lending_indexer.py docstring)",
            "warn",
        )
        return []
