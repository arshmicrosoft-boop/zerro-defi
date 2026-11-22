use crate::types::{CandidateCall, HarvestTarget};
use alloy_primitives::{Address, U256};
use alloy_sol_types::{sol, SolCall};
use std::str::FromStr;

sol! {
    function executeHarvestBounty(address strategy, uint256 minBountyWeth) external returns (uint256 bounty);
}

pub fn build_harvest_candidates(executor: &str, targets: &[HarvestTarget]) -> Vec<CandidateCall> {
    let mut out = Vec::new();
    for t in targets {
        let Ok(strategy) = Address::from_str(&t.strategy) else { continue };
        let Ok(min_bounty) = U256::from_str(&t.min_bounty_weth) else { continue };

        let call = executeHarvestBountyCall { strategy, minBountyWeth: min_bounty };
        let encoded = call.abi_encode();
        out.push(CandidateCall {
            pipeline: "HarvestBounty".to_string(),
            description: format!("Vault harvest bounty on {}", t.strategy),
            target_executor: executor.to_string(),
            calldata_hex: format!("0x{}", hex::encode(encoded)),
            min_profit_raw: t.min_bounty_weth.clone(),
            profit_token: if t.profit_token.is_empty() {
                "0x4200000000000000000000000000000000000006".to_string()
            } else {
                t.profit_token.clone()
            },
            profit_decimals: t.profit_decimals,
            token_price_usd: t.token_price_usd,
        });
    }
    out
}
