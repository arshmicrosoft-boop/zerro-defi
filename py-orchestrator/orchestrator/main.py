import asyncio
import os
import time
from pathlib import Path

from .config import (
    BASE_RPC_URLS,
    DRY_RUN,
    EXECUTOR_ADDRESS,
    MIN_PROFIT_USD,
    OPERATOR_PRIVATE_KEY,
    ROOT_DIR,
    TARGETS_FILE,
    VERIFIED_OUTBOX_FILE,
)
from .indexer import BaseStateIndexer
from .monitor import print_telemetry_dashboard
from .submitter import process_verified_outbox
from .state_store import ExecutionStateStore


class Config:
    def __init__(self):
        self.base_rpc_url = BASE_RPC_URLS[0]
        self.chain_id = 8453
        self.paymaster_bundler_rpc = os.getenv("PAYMASTER_BUNDLER_RPC", "")
        self.entrypoint = os.getenv("ENTRYPOINT_ADDRESS", "0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789")
        self.smart_account = os.getenv("SMART_ACCOUNT_ADDRESS", "")
        self.executor = EXECUTOR_ADDRESS
        self.owner_private_key = OPERATOR_PRIVATE_KEY
        self.targets_path = str(TARGETS_FILE)
        self.outbox_path = str(VERIFIED_OUTBOX_FILE)
        self.sync_interval_seconds = int(os.getenv("SYNC_INTERVAL_SECONDS", "60"))
        self.dry_run = DRY_RUN
        self.max_gas_per_op = int(os.getenv("MAX_GAS_PER_OP", "900000"))
        self.min_profit_wei = int(os.getenv("MIN_PROFIT_WEI", "15000000000000"))
        self.min_profit_usd = MIN_PROFIT_USD


async def main() -> None:
    cfg = Config()
    state = ExecutionStateStore(ROOT_DIR / "ops_state.db")
    print("[orchestrator] starting")
    print(f"[orchestrator] chain={cfg.chain_id} dry_run={cfg.dry_run} targets={cfg.targets_path}")

    indexer = BaseStateIndexer()
    last_sync = 0.0
    while True:
        now = time.time()
        if now - last_sync >= cfg.sync_interval_seconds:
            try:
                await indexer.update_targets_file()
            except Exception as e:
                print(f"[indexer] sync error: {e}")
            last_sync = now

        try:
            await process_verified_outbox()
        except Exception as e:
            print(f"[submitter] loop error: {e}")

        await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
