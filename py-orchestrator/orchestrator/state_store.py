import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class ExecutionStateStore:
    def __init__(
        self,
        db_path: Path,
        max_consecutive_failures: int = 5,
        cooldown_sec: int = 600,
    ) -> None:
        self.db_path = db_path
        self.max_consecutive_failures = max_consecutive_failures
        self.cooldown_sec = cooldown_sec
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    idempotency_key TEXT PRIMARY KEY,
                    pipeline TEXT NOT NULL,
                    description TEXT NOT NULL,
                    block_bucket INTEGER NOT NULL,
                    profit_token TEXT NOT NULL,
                    simulated_profit_raw TEXT NOT NULL,
                    simulated_profit_usd REAL NOT NULL,
                    realized_profit_raw TEXT NOT NULL DEFAULT '0',
                    realized_profit_usd REAL NOT NULL DEFAULT 0.0,
                    user_op_hash TEXT,
                    tx_hash TEXT,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_user_ops (
                    user_op_hash TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    inner_calldata_hex TEXT NOT NULL DEFAULT '0x',
                    min_profit_raw TEXT NOT NULL DEFAULT '0',
                    max_fee_per_gas TEXT NOT NULL DEFAULT '0x0',
                    max_priority_fee_per_gas TEXT NOT NULL DEFAULT '0x0',
                    bump_count INTEGER NOT NULL DEFAULT 0,
                    profit_token TEXT NOT NULL,
                    profit_decimals INTEGER NOT NULL,
                    token_price_usd REAL NOT NULL,
                    submitted_at INTEGER NOT NULL
                )
                """
            )
            existing_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(pending_user_ops)").fetchall()
            }
            migrations = {
                "inner_calldata_hex": "TEXT NOT NULL DEFAULT '0x'",
                "min_profit_raw": "TEXT NOT NULL DEFAULT '0'",
                "max_fee_per_gas": "TEXT NOT NULL DEFAULT '0x0'",
                "max_priority_fee_per_gas": "TEXT NOT NULL DEFAULT '0x0'",
                "bump_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for col, col_def in migrations.items():
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE pending_user_ops ADD COLUMN {col} {col_def}")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS circuit_breaker (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    tripped_until INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO circuit_breaker (id, consecutive_failures, tripped_until) VALUES (1, 0, 0)"
            )
            conn.commit()

    @staticmethod
    def compute_idempotency_key(pipeline: str, target_calldata: str, block_number: int) -> str:
        block_bucket = block_number // 5
        raw = f"{pipeline}:{target_calldata.lower()}:{block_bucket}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_circuit_open(self) -> Tuple[bool, str]:
        now = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT consecutive_failures, tripped_until, last_error FROM circuit_breaker WHERE id = 1"
            ).fetchone()
            if not row:
                return (False, "")
            failures, tripped_until, last_error = row
            if tripped_until > now:
                return (
                    True,
                    f"Circuit open for {tripped_until - now}s (failures={failures}, last_error={last_error})",
                )
            return (False, "")

    def has_pending_user_ops(self) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM pending_user_ops").fetchone()
            return bool(row and row[0] > 0)

    def get_pending_user_ops(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT user_op_hash, idempotency_key, sender, nonce,
                       inner_calldata_hex, min_profit_raw, max_fee_per_gas, max_priority_fee_per_gas,
                       bump_count, profit_token, profit_decimals, token_price_usd, submitted_at
                FROM pending_user_ops
                """
            ).fetchall()
            return [
                {
                    "user_op_hash": r[0],
                    "idempotency_key": r[1],
                    "sender": r[2],
                    "nonce": r[3],
                    "inner_calldata_hex": r[4],
                    "min_profit_raw": r[5],
                    "max_fee_per_gas": r[6],
                    "max_priority_fee_per_gas": r[7],
                    "bump_count": int(r[8]),
                    "profit_token": r[9],
                    "profit_decimals": int(r[10]),
                    "token_price_usd": float(r[11]),
                    "submitted_at": int(r[12]),
                }
                for r in rows
            ]

    def record_pending_user_op(
        self,
        user_op_hash: str,
        idempotency_key: str,
        sender: str,
        nonce: str,
        inner_calldata_hex: str,
        min_profit_raw: str,
        max_fee_per_gas: str,
        max_priority_fee_per_gas: str,
        bump_count: int,
        profit_token: str,
        profit_decimals: int,
        token_price_usd: float,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM pending_user_ops WHERE sender = ? AND nonce = ?", (sender, nonce))
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_user_ops
                (user_op_hash, idempotency_key, sender, nonce, inner_calldata_hex, min_profit_raw,
                 max_fee_per_gas, max_priority_fee_per_gas, bump_count, profit_token, profit_decimals,
                 token_price_usd, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_op_hash,
                    idempotency_key,
                    sender,
                    nonce,
                    inner_calldata_hex,
                    str(min_profit_raw),
                    max_fee_per_gas,
                    max_priority_fee_per_gas,
                    bump_count,
                    profit_token,
                    profit_decimals,
                    token_price_usd,
                    int(time.time()),
                ),
            )
            conn.commit()

    def clear_pending_user_op(self, user_op_hash: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM pending_user_ops WHERE user_op_hash = ?", (user_op_hash,))
            conn.commit()

    def is_duplicate(self, idempotency_key: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM executions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return row is not None

    def record_attempt(
        self,
        idempotency_key: str,
        pipeline: str,
        description: str,
        block_number: int,
        profit_token: str,
        simulated_profit_raw: str,
        simulated_profit_usd: float,
        status: str = "PENDING",
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO executions
                (idempotency_key, pipeline, description, block_bucket, profit_token,
                 simulated_profit_raw, simulated_profit_usd, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    pipeline,
                    description,
                    block_number // 5,
                    profit_token,
                    str(simulated_profit_raw),
                    float(simulated_profit_usd),
                    status,
                    int(time.time()),
                ),
            )
            conn.commit()

    def record_success(
        self,
        idempotency_key: str,
        user_op_hash: str,
        tx_hash: str,
        realized_profit_raw: str = "0",
        realized_profit_usd: float = 0.0,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE executions
                SET user_op_hash = ?, tx_hash = ?, realized_profit_raw = ?, realized_profit_usd = ?, status = 'CONFIRMED'
                WHERE idempotency_key = ?
                """,
                (
                    user_op_hash,
                    tx_hash,
                    str(realized_profit_raw),
                    float(realized_profit_usd),
                    idempotency_key,
                ),
            )
            conn.execute(
                "UPDATE circuit_breaker SET consecutive_failures = 0, tripped_until = 0, last_error = NULL WHERE id = 1"
            )
            conn.execute("DELETE FROM pending_user_ops WHERE user_op_hash = ?", (user_op_hash,))
            conn.commit()

    def record_failure(
        self, idempotency_key: str, error_msg: str, user_op_hash: Optional[str] = None
    ) -> None:
        now = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE executions SET user_op_hash = ?, status = ? WHERE idempotency_key = ?",
                (user_op_hash, f"FAILED: {error_msg[:120]}", idempotency_key),
            )
            if user_op_hash:
                conn.execute("DELETE FROM pending_user_ops WHERE user_op_hash = ?", (user_op_hash,))
            row = conn.execute(
                "SELECT consecutive_failures FROM circuit_breaker WHERE id = 1"
            ).fetchone()
            failures = (row[0] if row else 0) + 1
            tripped_until = now + self.cooldown_sec if failures >= self.max_consecutive_failures else 0
            conn.execute(
                """
                UPDATE circuit_breaker
                SET consecutive_failures = ?, tripped_until = ?, last_error = ?
                WHERE id = 1
                """,
                (failures, tripped_until, error_msg[:200]),
            )
            conn.commit()
