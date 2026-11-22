import asyncio
import json
import os
import time
from typing import Dict, List, Set, Tuple
import aiohttp
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import to_checksum_address

from . import ui
from .config import (
    BASE_RPC_URLS,
    BEEFY_BASE_VAULTS_API,
    MIN_PROFIT_USD,
    MORPHO_BLUE,
    MULTICALL3,
    TARGETS_FILE,
    USDC_BASE,
    WETH_BASE,
)

MORPHO_GRAPHQL_API = "https://blue-api.morpho.org/graphql"
MORPHO_BORROW_TOPIC = "0x570954540bed6b1304a87dfe815a5eda4a648f7097a16240dcd85c9b5fd42a43"

SEL_ID_TO_MARKET_PARAMS = bytes.fromhex("2c3c9157")
SEL_MARKET = bytes.fromhex("5c60e39a")
SEL_POSITION = bytes.fromhex("93c52062")
SEL_ORACLE_PRICE = bytes.fromhex("a035b1fe")
SEL_LATEST_ROUND_DATA = bytes.fromhex("feaf968c")
SEL_BORROW_RATE_VIEW = bytes.fromhex("8c004f09")
SEL_TRY_AGGREGATE = bytes.fromhex("bce38bd7")
SEL_DECIMALS = bytes.fromhex("313ce567")
SEL_BALANCE_OF = bytes.fromhex("70a08231")

UNISWAP_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
SEL_QUOTE_EXACT_INPUT_SINGLE = bytes.fromhex("c6a5026a")
UNI_FEE_TIERS = [100, 500, 3000, 10000]

MAX_ORACLE_AGE_SEC = int(os.getenv("MAX_ORACLE_AGE_SEC", "7200"))
MIN_POSITION_USD = float(os.getenv("MIN_POSITION_USD", "2.00"))
SWAP_HAIRCUT_BPS = int(os.getenv("SWAP_HAIRCUT_BPS", "9950"))
WATCHLIST_HF_CEILING = float(os.getenv("WATCHLIST_HF_CEILING", "1.08"))

DENYLIST_TOKENS: Set[str] = {
    t.strip().lower()
    for t in os.getenv("DENYLIST_TOKENS", "").split(",")
    if t.strip()
}

WAD = 10**18
ORACLE_PRICE_SCALE = 10**36
VIRTUAL_SHARES = 10**6
VIRTUAL_ASSETS = 1
LIQUIDATION_CURSOR = 3 * 10**17
MAX_LIF = 115 * 10**16


def mul_div_down(x: int, y: int, d: int) -> int:
    return (x * y) // d if d > 0 else 0


def mul_div_up(x: int, y: int, d: int) -> int:
    return (x * y + (d - 1)) // d if d > 0 else 0


def w_taylor_compounded(x: int, n: int) -> int:
    first_term = x * n
    second_term = mul_div_down(first_term, first_term, 2 * WAD)
    third_term = mul_div_down(second_term, first_term, 3 * WAD)
    return first_term + second_term + third_term


def accrue_morpho_interest(
    total_borrow_assets: int,
    borrow_rate_per_sec: int,
    last_update: int,
    current_timestamp: int,
) -> int:
    if current_timestamp <= last_update or total_borrow_assets == 0 or borrow_rate_per_sec == 0:
        return total_borrow_assets
    elapsed = current_timestamp - last_update
    interest = mul_div_down(
        total_borrow_assets, w_taylor_compounded(borrow_rate_per_sec, elapsed), WAD
    )
    return total_borrow_assets + interest


def to_assets_up(shares: int, total_assets: int, total_shares: int) -> int:
    return mul_div_up(shares, total_assets + VIRTUAL_ASSETS, total_shares + VIRTUAL_SHARES)


def compute_lif(lltv: int) -> int:
    denom = WAD - ((LIQUIDATION_CURSOR * (WAD - lltv)) // WAD)
    if denom == 0:
        return MAX_LIF
    lif = (WAD * WAD) // denom
    return min(MAX_LIF, lif)


def compute_morpho_liquidation_params(
    borrow_shares: int,
    collateral: int,
    total_borrow_assets: int,
    total_borrow_shares: int,
    oracle_price: int,
    lltv: int,
) -> Tuple[bool, float, int, int, int]:
    if borrow_shares == 0 or collateral == 0 or oracle_price == 0 or lltv == 0:
        return (False, 999.0, 0, 0, 0)

    borrowed_assets = to_assets_up(borrow_shares, total_borrow_assets, total_borrow_shares)
    if borrowed_assets == 0:
        return (False, 999.0, 0, 0, 0)

    max_borrow = (collateral * oracle_price // ORACLE_PRICE_SCALE) * lltv // WAD
    hf = max_borrow / borrowed_assets

    if hf > WATCHLIST_HF_CEILING:
        return (False, hf, 0, 0, 0)

    lif = compute_lif(lltv)
    seized_for_full_debt = mul_div_down(
        mul_div_up(borrowed_assets, lif, WAD),
        ORACLE_PRICE_SCALE,
        oracle_price,
    )

    if seized_for_full_debt <= collateral:
        return (True, hf, 0, borrow_shares, borrowed_assets)
    else:
        seized_valued = mul_div_up(collateral, oracle_price, ORACLE_PRICE_SCALE)
        repaid_assets_bounded = mul_div_up(seized_valued, WAD, lif)
        return (True, hf, collateral, 0, repaid_assets_bounded)


class BaseStateIndexer:
    def __init__(self) -> None:
        self.rpc_idx = 0
        self.tracked_positions: Dict[Tuple[str, str], None] = {}
        self.market_params_cache: Dict[str, Tuple[str, str, str, str, int]] = {}
        self.token_decimals_cache: Dict[str, int] = {
            USDC_BASE.lower(): 6,
            WETH_BASE.lower(): 18,
        }
        self.token_usd_price_cache: Dict[str, Tuple[float, float]] = {
            USDC_BASE.lower(): (1.0, time.time() + 86400),
        }
        self.last_scanned_block: int = 0

    async def _rpc_call(self, session: aiohttp.ClientSession, method: str, params: list):
        for attempt in range(len(BASE_RPC_URLS) * 2):
            url = BASE_RPC_URLS[(self.rpc_idx + attempt) % len(BASE_RPC_URLS)]
            try:
                async with session.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    data = await resp.json()
                    if "result" in data and data["result"] is not None:
                        return data["result"]
            except Exception:
                continue
        return None

    async def _multicall(
        self, session: aiohttp.ClientSession, calls: List[Tuple[str, bytes]]
    ) -> List[Tuple[bool, bytes]]:
        if not calls:
            return []
        encoded_calls = [(to_checksum_address(target), data) for target, data in calls]
        calldata = "0x" + (
            SEL_TRY_AGGREGATE + abi_encode(["bool", "(address,bytes)[]"], [False, encoded_calls])
        ).hex()
        res = await self._rpc_call(
            session, "eth_call", [{"to": MULTICALL3, "data": calldata}, "latest"]
        )
        if not res or res == "0x":
            return [(False, b"") for _ in calls]
        try:
            raw = bytes.fromhex(res[2:])
            decoded = abi_decode(["(bool,bytes)[]"], raw)[0]
            return [(bool(item[0]), bytes(item[1])) for item in decoded]
        except Exception:
            return [(False, b"") for _ in calls]

    async def get_token_decimals(
        self, session: aiohttp.ClientSession, tokens: List[str]
    ) -> Dict[str, int]:
        missing = [t for t in set(tokens) if t.lower() not in self.token_decimals_cache]
        if missing:
            calls = [(t, SEL_DECIMALS) for t in missing]
            results = await self._multicall(session, calls)
            for t, (ok, data) in zip(missing, results):
                if ok and len(data) >= 32:
                    dec = int(abi_decode(["uint8"], data[:32])[0])
                    self.token_decimals_cache[t.lower()] = dec if 0 < dec <= 36 else 18
                else:
                    self.token_decimals_cache[t.lower()] = 18
        return {t.lower(): self.token_decimals_cache.get(t.lower(), 18) for t in tokens}

    async def get_token_usd_price(self, session: aiohttp.ClientSession, token: str) -> float:
        t_low = token.lower()
        if t_low == USDC_BASE.lower():
            return 1.0
        cached = self.token_usd_price_cache.get(t_low)
        if cached and cached[1] > time.time():
            return cached[0]

        dec_map = await self.get_token_decimals(session, [token, USDC_BASE])
        t_dec = dec_map[t_low]
        one_unit = 10**t_dec

        _, usdc_out = await self._select_best_uni_v3_fee(session, token, USDC_BASE, one_unit)
        if usdc_out > 0:
            price = usdc_out / 1e6
            self.token_usd_price_cache[t_low] = (price, time.time() + 120)
            return price

        if t_low != WETH_BASE.lower():
            _, weth_out = await self._select_best_uni_v3_fee(session, token, WETH_BASE, one_unit)
            if weth_out > 0:
                weth_price = await self.get_token_usd_price(session, WETH_BASE)
                price = (weth_out / 1e18) * weth_price
                if price > 0:
                    self.token_usd_price_cache[t_low] = (price, time.time() + 120)
                    return price

        return 3000.0 if t_low == WETH_BASE.lower() else 1.0

    async def usd_to_token_raw(
        self, session: aiohttp.ClientSession, token: str, usd_amount: float
    ) -> Tuple[int, int, float]:
        dec_map = await self.get_token_decimals(session, [token])
        decimals = dec_map[token.lower()]
        price_usd = await self.get_token_usd_price(session, token)
        if price_usd <= 0:
            price_usd = 1.0
        raw_amount = max(1, int((usd_amount / price_usd) * (10**decimals)))
        return raw_amount, decimals, price_usd

    async def _select_best_uni_v3_fee(
        self,
        session: aiohttp.ClientSession,
        token_in: str,
        token_out: str,
        amount_in: int,
    ) -> Tuple[int, int]:
        if amount_in <= 0:
            return (3000, 0)
        if token_in.lower() == token_out.lower():
            return (100, amount_in)
        calls = []
        for fee in UNI_FEE_TIERS:
            call_data = SEL_QUOTE_EXACT_INPUT_SINGLE + abi_encode(
                ["(address,address,uint256,uint24,uint160)"],
                [(to_checksum_address(token_in), to_checksum_address(token_out), amount_in, fee, 0)],
            )
            calls.append((UNISWAP_QUOTER_V2, call_data))

        results = await self._multicall(session, calls)
        best_fee = 3000
        best_amount_out = 0
        for fee, (ok, data) in zip(UNI_FEE_TIERS, results):
            if ok and len(data) >= 32:
                try:
                    amount_out = abi_decode(["uint256"], data[:32])[0]
                    if amount_out > best_amount_out:
                        best_amount_out = int(amount_out)
                        best_fee = fee
                except Exception:
                    continue
        return (best_fee, best_amount_out)

    async def fetch_beefy_harvest_targets(self, session: aiohttp.ClientSession) -> List[dict]:
        targets = []
        try:
            async with session.get(
                BEEFY_BASE_VAULTS_API, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return []
                vaults = await resp.json()

            active_strats = [
                v.get("strategy")
                for v in vaults
                if v.get("status") == "active"
                and v.get("chain") == "base"
                and v.get("strategy")
                and len(v.get("strategy", "")) == 42
            ][:80]

            min_bounty_weth, weth_dec, weth_price = await self.usd_to_token_raw(
                session, WETH_BASE, MIN_PROFIT_USD
            )

            for strat in active_strats:
                targets.append(
                    {
                        "strategy": to_checksum_address(strat),
                        "min_bounty_weth": str(min_bounty_weth),
                        "profit_token": WETH_BASE,
                        "profit_decimals": weth_dec,
                        "token_price_usd": weth_price,
                    }
                )
        except Exception as e:
            ui.log("indexer", f"beefy api warning: {e}", "warn")
        return targets

    async def _discover_morpho_borrowers_via_graphql(self, session: aiohttp.ClientSession) -> int:
        query = """
        query {
          marketPositions(
            first: 150
            where: { chainId_in: [8453], borrowShares_gte: "1000" }
            orderBy: SupplyShares
            orderDirection: asc
          ) {
            items {
              user { address }
              market { uniqueKey }
            }
          }
        }
        """
        added = 0
        try:
            async with session.post(
                MORPHO_GRAPHQL_API,
                json={"query": query},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    items = (
                        body.get("data", {})
                        .get("marketPositions", {})
                        .get("items", [])
                    )
                    for item in items:
                        m_key = item.get("market", {}).get("uniqueKey", "")
                        u_addr = item.get("user", {}).get("address", "")
                        if m_key.startswith("0x") and len(m_key) == 66 and len(u_addr) == 42:
                            self.tracked_positions[(m_key, to_checksum_address(u_addr))] = None
                            added += 1
        except Exception:
            pass
        return added

    async def scan_morpho_candidates(self, session: aiohttp.ClientSession) -> List[dict]:
        block_res = await self._rpc_call(session, "eth_getBlockByNumber", ["latest", False])
        if not block_res:
            return []
        latest_block = int(block_res["number"], 16)
        latest_timestamp = int(block_res["timestamp"], 16)

        await self._discover_morpho_borrowers_via_graphql(session)

        from_block = (
            max(latest_block - 600, self.last_scanned_block + 1)
            if self.last_scanned_block > 0
            else latest_block - 600
        )
        cur = from_block
        while cur <= latest_block:
            end_b = min(cur + 299, latest_block)
            logs = await self._rpc_call(
                session,
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(cur),
                        "toBlock": hex(end_b),
                        "address": MORPHO_BLUE,
                        "topics": [MORPHO_BORROW_TOPIC],
                    }
                ],
            )
            if isinstance(logs, list):
                for log in logs:
                    topics = log.get("topics", [])
                    if len(topics) >= 3:
                        market_id = topics[1]
                        borrower = "0x" + topics[2][-40:]
                        self.tracked_positions[(market_id, to_checksum_address(borrower))] = None
            cur = end_b + 1

        self.last_scanned_block = latest_block

        if not self.tracked_positions:
            return []

        unique_markets = list({m_id for (m_id, _) in self.tracked_positions.keys()})
        missing_markets = [m for m in unique_markets if m not in self.market_params_cache]

        if missing_markets:
            mp_calls = [
                (MORPHO_BLUE, SEL_ID_TO_MARKET_PARAMS + bytes.fromhex(m[2:]))
                for m in missing_markets
            ]
            mp_results = await self._multicall(session, mp_calls)
            for m_id, (ok, data) in zip(missing_markets, mp_results):
                if ok and len(data) >= 160:
                    decoded = abi_decode(
                        ["address", "address", "address", "address", "uint256"], data
                    )
                    self.market_params_cache[m_id] = decoded

        valid_markets = [m for m in unique_markets if m in self.market_params_cache]
        state_calls = [(MORPHO_BLUE, SEL_MARKET + bytes.fromhex(m[2:])) for m in valid_markets]
        state_results = await self._multicall(session, state_calls)

        market_raw_states: Dict[str, Tuple[int, int, int, int, int, int]] = {}
        irm_calls = []
        irm_markets = []
        for m_id, (ok, data) in zip(valid_markets, state_results):
            if ok and len(data) >= 192:
                s = abi_decode(
                    ["uint128", "uint128", "uint128", "uint128", "uint128", "uint128"], data
                )
                market_raw_states[m_id] = s
                loan_token, coll_token, oracle, irm, lltv = self.market_params_cache[m_id]
                if int(irm, 16) != 0:
                    irm_call = SEL_BORROW_RATE_VIEW + abi_encode(
                        [
                            "(address,address,address,address,uint256)",
                            "(uint128,uint128,uint128,uint128,uint128,uint128)",
                        ],
                        [
                            (
                                to_checksum_address(loan_token),
                                to_checksum_address(coll_token),
                                to_checksum_address(oracle),
                                to_checksum_address(irm),
                                int(lltv),
                            ),
                            tuple(int(x) for x in s),
                        ],
                    )
                    irm_calls.append((irm, irm_call))
                    irm_markets.append(m_id)

        irm_results = await self._multicall(session, irm_calls) if irm_calls else []
        borrow_rates: Dict[str, int] = {}
        for m_id, (ok, data) in zip(irm_markets, irm_results):
            if ok and len(data) >= 32:
                borrow_rates[m_id] = int(abi_decode(["uint256"], data[:32])[0])

        market_totals: Dict[str, Tuple[int, int]] = {}
        oracle_addresses: List[str] = []
        for m_id, s in market_raw_states.items():
            total_borrow_assets = accrue_morpho_interest(
                int(s[2]), borrow_rates.get(m_id, 0), int(s[4]), latest_timestamp
            )
            market_totals[m_id] = (total_borrow_assets, int(s[3]))
            oracle_addr = self.market_params_cache[m_id][2]
            if int(oracle_addr, 16) != 0 and oracle_addr not in oracle_addresses:
                oracle_addresses.append(oracle_addr)

        oracle_calls = []
        for o_addr in oracle_addresses:
            oracle_calls.append((o_addr, SEL_ORACLE_PRICE))
            oracle_calls.append((o_addr, SEL_LATEST_ROUND_DATA))

        oracle_results = await self._multicall(session, oracle_calls)
        oracle_prices: Dict[str, int] = {}
        stale_oracles: Set[str] = set()

        for idx, o_addr in enumerate(oracle_addresses):
            ok_p, data_p = oracle_results[idx * 2]
            ok_r, data_r = oracle_results[idx * 2 + 1]
            if ok_r and len(data_r) >= 160:
                try:
                    round_id, answer, _, updated_at, answered_in_round = abi_decode(
                        ["uint80", "int256", "uint256", "uint256", "uint80"], data_r[:160]
                    )
                    if (
                        answer <= 0
                        or answered_in_round < round_id
                        or (
                            latest_timestamp > int(updated_at)
                            and latest_timestamp - int(updated_at) > MAX_ORACLE_AGE_SEC
                        )
                    ):
                        stale_oracles.add(o_addr.lower())
                        continue
                except Exception:
                    pass
            if ok_p and len(data_p) >= 32:
                p_val = int(abi_decode(["uint256"], data_p[:32])[0])
                if p_val > 0:
                    oracle_prices[o_addr.lower()] = p_val

        pos_keys = list(self.tracked_positions.keys())[:250]
        pos_calls = [
            (MORPHO_BLUE, SEL_POSITION + abi_encode(["bytes32", "address"], [bytes.fromhex(m[2:]), b]))
            for (m, b) in pos_keys
        ]
        pos_results = await self._multicall(session, pos_calls)

        candidates = []
        for (m_id, borrower), (ok, data) in zip(pos_keys, pos_results):
            if not ok or len(data) < 96 or m_id not in self.market_params_cache or m_id not in market_totals:
                continue
            _, borrow_shares, collateral = abi_decode(["uint256", "uint128", "uint128"], data)
            if borrow_shares == 0 or collateral == 0:
                continue

            loan_token, coll_token, oracle, irm, lltv = self.market_params_cache[m_id]
            if (
                int(loan_token, 16) == 0
                or int(coll_token, 16) == 0
                or loan_token.lower() in DENYLIST_TOKENS
                or coll_token.lower() in DENYLIST_TOKENS
                or oracle.lower() in stale_oracles
            ):
                continue

            oracle_price = oracle_prices.get(oracle.lower(), 0)
            total_borrow_assets, total_borrow_shares = market_totals[m_id]

            is_liq, hf, seized_assets, repaid_shares, expected_repaid = compute_morpho_liquidation_params(
                int(borrow_shares),
                int(collateral),
                total_borrow_assets,
                total_borrow_shares,
                oracle_price,
                int(lltv),
            )
            if not is_liq:
                continue

            min_profit_raw, loan_dec, loan_usd_price = await self.usd_to_token_raw(
                session, loan_token, MIN_PROFIT_USD
            )

            expected_repaid_usd = (expected_repaid / (10**loan_dec)) * loan_usd_price
            if expected_repaid_usd < MIN_POSITION_USD:
                continue

            quote_collateral_in = seized_assets if seized_assets > 0 else int(collateral)
            best_fee, quoted_loan_out = await self._select_best_uni_v3_fee(
                session, coll_token, loan_token, quote_collateral_in
            )
            if quoted_loan_out == 0:
                continue

            haircut_loan_out = (quoted_loan_out * SWAP_HAIRCUT_BPS) // 10000
            if hf <= 1.0 and haircut_loan_out < expected_repaid + min_profit_raw:
                continue

            candidates.append(
                {
                    "loan_token": to_checksum_address(loan_token),
                    "collateral_token": to_checksum_address(coll_token),
                    "oracle": to_checksum_address(oracle),
                    "irm": to_checksum_address(irm),
                    "lltv": str(lltv),
                    "borrower": to_checksum_address(borrower),
                    "seized_assets": str(seized_assets),
                    "repaid_shares": str(repaid_shares),
                    "swap_fee": best_fee,
                    "min_profit_raw": str(min_profit_raw),
                    "profit_token": to_checksum_address(loan_token),
                    "profit_decimals": loan_dec,
                    "token_price_usd": loan_usd_price,
                    "health_factor": round(hf, 6),
                }
            )
        return candidates

    async def discover_dutch_auctions(self, session: aiohttp.ClientSession) -> List[dict]:
        raw_cfg = os.getenv("DUTCH_AUCTION_CONTRACTS_JSON", "[]")
        try:
            auctions = json.loads(raw_cfg)
        except Exception:
            auctions = []

        if not auctions:
            return []

        dutch_targets = []
        for item in auctions:
            auction_addr = item.get("auction_contract")
            pay_token = item.get("pay_token", USDC_BASE)
            reward_tokens = item.get("reward_tokens", [])
            current_price = int(item.get("current_price_raw", "0"))
            if not auction_addr or not reward_tokens or current_price <= 0:
                continue

            bal_calls = [
                (rt, SEL_BALANCE_OF + abi_encode(["address"], [to_checksum_address(auction_addr)]))
                for rt in reward_tokens
            ]
            bal_results = await self._multicall(session, bal_calls)

            swap_fees = []
            min_amounts_out = []
            total_expected_pay_out = 0
            valid = True

            for rt, (ok, data) in zip(reward_tokens, bal_results):
                if not ok or len(data) < 32 or rt.lower() in DENYLIST_TOKENS:
                    valid = False
                    break
                r_bal = int(abi_decode(["uint256"], data[:32])[0])
                if r_bal == 0:
                    valid = False
                    break
                fee, quoted_out = await self._select_best_uni_v3_fee(session, rt, pay_token, r_bal)
                if quoted_out == 0:
                    valid = False
                    break
                swap_fees.append(fee)
                min_out = (quoted_out * SWAP_HAIRCUT_BPS) // 10000
                min_amounts_out.append(str(min_out))
                total_expected_pay_out += min_out

            if not valid:
                continue

            min_profit_raw, pay_dec, pay_usd = await self.usd_to_token_raw(
                session, pay_token, MIN_PROFIT_USD
            )
            if total_expected_pay_out < current_price + min_profit_raw:
                continue

            dutch_targets.append(
                {
                    "auction_contract": to_checksum_address(auction_addr),
                    "pay_token": to_checksum_address(pay_token),
                    "flash_amount": str(current_price),
                    "reward_tokens": [to_checksum_address(r) for r in reward_tokens],
                    "max_auction_price": str(current_price),
                    "swap_fees": swap_fees,
                    "min_amounts_out": min_amounts_out,
                    "min_profit_raw": str(min_profit_raw),
                    "profit_token": to_checksum_address(pay_token),
                    "profit_decimals": pay_dec,
                    "token_price_usd": pay_usd,
                }
            )
        return dutch_targets

    async def update_targets_file(self) -> dict:
        started = time.perf_counter()
        async with aiohttp.ClientSession() as session:
            harvest_targets, morpho_targets, dutch_targets = await asyncio.gather(
                self.fetch_beefy_harvest_targets(session),
                self.scan_morpho_candidates(session),
                self.discover_dutch_auctions(session),
            )
            payload = {
                "updated_at": int(time.time()),
                "morpho_targets": morpho_targets,
                "dutch_targets": dutch_targets,
                "harvest_targets": harvest_targets,
            }
            TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = TARGETS_FILE.with_suffix(".tmp")
            tmp_file.write_text(json.dumps(payload, indent=2))
            tmp_file.replace(TARGETS_FILE)

        summary = {
            "morpho": len(morpho_targets),
            "dutch": len(dutch_targets),
            "harvest": len(harvest_targets),
            "tracked": len(self.tracked_positions),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        ui.log(
            "indexer",
            f"tracked={summary['tracked']} · morpho={summary['morpho']} "
            f"(HF<={WATCHLIST_HF_CEILING}) · dutch={summary['dutch']} "
            f"· harvest={summary['harvest']} · {summary['duration_ms']}ms",
            "info",
        )
        return summary
