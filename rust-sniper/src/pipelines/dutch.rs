use crate::types::{CandidateCall, DutchAuctionTarget};
use alloy_primitives::{Address, U256};
use alloy_sol_types::{sol, SolCall};
use std::str::FromStr;

sol! {
    function executeDutchAuction(
        address auctionContract,
        address payToken,
        uint256 flashAmount,
        address[] calldata rewardTokens,
        uint256 maxAuctionPrice,
        uint24[] calldata swapFees,
        uint256[] calldata minAmountsOut,
        uint256 minProfit
    ) external returns (uint256 profit);
}

pub fn build_dutch_candidates(executor: &str, targets: &[DutchAuctionTarget]) -> Vec<CandidateCall> {
    let mut out = Vec::new();
    for t in targets {
        let Ok(auction_contract) = Address::from_str(&t.auction_contract) else { continue };
        let Ok(pay_token) = Address::from_str(&t.pay_token) else { continue };
        let Ok(flash_amount) = U256::from_str(&t.flash_amount) else { continue };
        let Ok(max_auction_price) = U256::from_str(&t.max_auction_price) else { continue };
        let Ok(min_profit) = U256::from_str(&t.min_profit_raw) else { continue };

        let mut reward_addrs = Vec::new();
        for r in &t.reward_tokens {
            if let Ok(a) = Address::from_str(r) { reward_addrs.push(a); }
        }
        let mut min_outs = Vec::new();
        for m in &t.min_amounts_out {
            if let Ok(v) = U256::from_str(m) { min_outs.push(v); }
        }
        if reward_addrs.len() != t.swap_fees.len() || reward_addrs.len() != min_outs.len() {
            continue;
        }

        let call = executeDutchAuctionCall {
            auctionContract: auction_contract,
            payToken: pay_token,
            flashAmount: flash_amount,
            rewardTokens: reward_addrs,
            maxAuctionPrice: max_auction_price,
            swapFees: t.swap_fees.clone(),
            minAmountsOut: min_outs,
            minProfit: min_profit,
        };

        let encoded = call.abi_encode();
        out.push(CandidateCall {
            pipeline: "DutchAuction".to_string(),
            description: format!("Dutch auction flash-clear {}", t.auction_contract),
            target_executor: executor.to_string(),
            calldata_hex: format!("0x{}", hex::encode(encoded)),
            min_profit_raw: t.min_profit_raw.clone(),
            profit_token: if t.profit_token.is_empty() { t.pay_token.clone() } else { t.profit_token.clone() },
            profit_decimals: t.profit_decimals,
            token_price_usd: t.token_price_usd,
        });
    }
    out
}
