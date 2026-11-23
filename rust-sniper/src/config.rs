use std::env;

#[derive(Clone, Debug)]
pub struct SniperConfig {
    pub rpc_urls: Vec<String>,
    pub executor_address: String,
    pub multicall3_address: String,
    pub targets_file: String,
    pub outbox_file: String,
    pub status_file: String,
    pub min_profit_usd: f64,
    pub poll_interval_ms: u64,
    pub ws_url: Option<String>,
}

impl SniperConfig {
    pub fn from_env() -> Self {
        dotenvy::dotenv().ok();
        let rpc_urls = env::var("BASE_RPC_URLS")
            .unwrap_or_else(|_| "https://base-rpc.publicnode.com,https://mainnet.base.org".into())
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        Self {
            rpc_urls,
            executor_address: env::var("EXECUTOR_ADDRESS").unwrap_or_else(|_| "0x0000000000000000000000000000000000000000".into()),
            multicall3_address: env::var("MULTICALL3_ADDRESS").unwrap_or_else(|_| "0xcA11bde05977b3631167028862bE2a173976CA11".into()),
            targets_file: env::var("TARGETS_PATH").unwrap_or_else(|_| "../targets.json".into()),
            outbox_file: env::var("OUTBOX_PATH").unwrap_or_else(|_| "../verified_outbox.json".into()),
            status_file: env::var("STATUS_PATH").unwrap_or_else(|_| "../runtime/sniper_status.json".into()),
            min_profit_usd: env::var("MIN_PROFIT_USD").ok().and_then(|v| v.parse().ok()).unwrap_or(0.05),
            poll_interval_ms: env::var("BLOCK_POLL_MS").ok().and_then(|v| v.parse().ok()).unwrap_or(500),
            ws_url: env::var("BASE_WS_URL")
                .ok()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty()),
        }
    }
}
