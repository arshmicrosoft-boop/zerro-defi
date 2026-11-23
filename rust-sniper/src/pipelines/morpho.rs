use crate::types::{CandidateCall, MorphoTarget};
use alloy_primitives::{Address, Bytes, U256};
use alloy_sol_types::{sol, SolCall};
use std::str::FromStr;

sol! {
    struct MarketParams {
        address loanToken;
        address collateralToken;
        address oracle;
        address irm;
        uint256 lltv;
    }

    function executeMorphoLiquidation(
        MarketParams calldata params,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        uint24 swapFee,
        uint256 minProfit
    ) external returns (uint256 profit);

    function executeMorphoLiquidationRouted(
        MarketParams calldata params,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        uint8 swapKind,
        uint24 uniFee,
        bytes calldata uniPath,
        address[] calldata aeroPath,
        bool[] calldata aeroStable,
        uint256 minProfit
    ) external returns (uint256 profit);
}

pub fn build_morpho_candidates(executor: &str, targets: &[MorphoTarget]) -> Vec<CandidateCall> {
    let mut out = Vec::new();
    for t in targets {
        let Ok(loan_token) = Address::from_str(&t.loan_token) else { continue };
        let Ok(collateral_token) = Address::from_str(&t.collateral_token) else { continue };
        let Ok(oracle) = Address::from_str(&t.oracle) else { continue };
        let Ok(irm) = Address::from_str(&t.irm) else { continue };
        let Ok(lltv) = U256::from_str(&t.lltv) else { continue };
        let Ok(borrower) = Address::from_str(&t.borrower) else { continue };
        let Ok(seized_assets) = U256::from_str(&t.seized_assets) else { continue };
        let Ok(repaid_shares) = U256::from_str(&t.repaid_shares) else { continue };
        let Ok(min_profit) = U256::from_str(&t.min_profit_raw) else { continue };

        let params = MarketParams {
            loanToken: loan_token,
            collateralToken: collateral_token,
            oracle,
            irm,
            lltv,
        };

        // `uni_multi` and `aero` need the routed entrypoint; everything else
        // (including legacy targets with no swap_kind field) uses the original
        // single-hop function.
        let encoded = if t.swap_kind == "uni_multi" || t.swap_kind == "aero" {
            let uni_path = if t.swap_path.is_empty() {
                Bytes::new()
            } else {
                match hex::decode(t.swap_path.trim_start_matches("0x")) {
                    Ok(v) => Bytes::from(v),
                    Err(_) => continue,
                }
            };

            let mut aero_path = Vec::with_capacity(t.swap_aero_tokens.len());
            let mut aero_ok = true;
            for s in &t.swap_aero_tokens {
                match Address::from_str(s) {
                    Ok(a) => aero_path.push(a),
                    Err(_) => {
                        aero_ok = false;
                        break;
                    }
                }
            }
            if !aero_ok {
                continue;
            }

            executeMorphoLiquidationRoutedCall {
                params,
                borrower,
                seizedAssets: seized_assets,
                repaidShares: repaid_shares,
                swapKind: if t.swap_kind == "aero" { 2 } else { 1 },
                uniFee: t.swap_fee,
                uniPath: uni_path,
                aeroPath: aero_path,
                aeroStable: t.swap_aero_stable.clone(),
                minProfit: min_profit,
            }
            .abi_encode()
        } else {
            executeMorphoLiquidationCall {
                params,
                borrower,
                seizedAssets: seized_assets,
                repaidShares: repaid_shares,
                swapFee: t.swap_fee,
                minProfit: min_profit,
            }
            .abi_encode()
        };

        out.push(CandidateCall {
            pipeline: "MorphoLiquidation".to_string(),
            // Borrower-only description so the 25/50/100% ladder collapses to a
            // single best-profit submission during dedupe.
            description: format!("Morpho liquidate borrower {}", t.borrower),
            target_executor: executor.to_string(),
            calldata_hex: format!("0x{}", hex::encode(encoded)),
            min_profit_raw: t.min_profit_raw.clone(),
            profit_token: if t.profit_token.is_empty() { t.loan_token.clone() } else { t.profit_token.clone() },
            profit_decimals: t.profit_decimals,
            token_price_usd: t.token_price_usd,
        });
    }
    out
}
