use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TargetsFile {
    #[serde(default)]
    pub updated_at: u64,
    #[serde(default)]
    pub morpho_targets: Vec<MorphoTarget>,
    #[serde(default)]
    pub dutch_targets: Vec<DutchAuctionTarget>,
    #[serde(default)]
    pub harvest_targets: Vec<HarvestTarget>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct MorphoTarget {
    pub loan_token: String,
    pub collateral_token: String,
    pub oracle: String,
    pub irm: String,
    pub lltv: String,
    pub borrower: String,
    pub seized_assets: String,
    pub repaid_shares: String,
    pub swap_fee: u32,
    #[serde(default)]
    pub swap_kind: String,
    #[serde(default)]
    pub swap_path: String,
    #[serde(default)]
    pub swap_aero_tokens: Vec<String>,
    #[serde(default)]
    pub swap_aero_stable: Vec<bool>,
    #[serde(default = "default_tier")]
    pub tier_pct: u32,
    pub min_profit_raw: String,
    #[serde(default)]
    pub profit_token: String,
    #[serde(default = "default_decimals")]
    pub profit_decimals: u32,
    #[serde(default = "default_price")]
    pub token_price_usd: f64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DutchAuctionTarget {
    pub auction_contract: String,
    pub pay_token: String,
    pub flash_amount: String,
    pub reward_tokens: Vec<String>,
    pub max_auction_price: String,
    pub swap_fees: Vec<u32>,
    pub min_amounts_out: Vec<String>,
    pub min_profit_raw: String,
    #[serde(default)]
    pub profit_token: String,
    #[serde(default = "default_decimals")]
    pub profit_decimals: u32,
    #[serde(default = "default_price")]
    pub token_price_usd: f64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct HarvestTarget {
    pub strategy: String,
    pub min_bounty_weth: String,
    #[serde(default)]
    pub profit_token: String,
    #[serde(default = "default_decimals")]
    pub profit_decimals: u32,
    #[serde(default = "default_price")]
    pub token_price_usd: f64,
}

fn default_decimals() -> u32 { 18 }
fn default_price() -> f64 { 1.0 }
fn default_tier() -> u32 { 100 }

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandidateCall {
    pub pipeline: String,
    pub description: String,
    pub target_executor: String,
    pub calldata_hex: String,
    pub min_profit_raw: String,
    pub profit_token: String,
    pub profit_decimals: u32,
    pub token_price_usd: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerifiedOpportunity {
    pub pipeline: String,
    pub description: String,
    pub executor_calldata: String,
    pub min_profit_raw: String,
    pub simulated_profit_raw: String,
    pub simulated_profit_usd: f64,
    pub profit_token: String,
    pub profit_decimals: u32,
    pub token_price_usd: f64,
    pub block_number: u64,
    pub timestamp: u64,
}
