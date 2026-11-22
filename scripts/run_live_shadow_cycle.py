import asyncio
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "py-orchestrator"))

os.environ["DRY_RUN"] = "true"

from orchestrator.indexer import BaseStateIndexer
from orchestrator.monitor import print_telemetry_dashboard
from orchestrator.submitter import process_verified_outbox


async def main() -> None:
    print("[ShadowRun] Connecting to live Base Mainnet RPC & syncing targets.json ...")
    indexer = BaseStateIndexer()
    await indexer.update_targets_file()
    await process_verified_outbox()
    print_telemetry_dashboard()


if __name__ == "__main__":
    asyncio.run(main())
