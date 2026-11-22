mod config;
mod pipelines;
mod sim;
mod types;

use config::SniperConfig;
use pipelines::{
    dutch::build_dutch_candidates, harvest::build_harvest_candidates,
    morpho::build_morpho_candidates,
};
use reqwest::Client;
use sim::{get_block_number, simulate_batch_on_multicall3};
use std::fs;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use types::{TargetsFile, VerifiedOpportunity};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cfg = SniperConfig::from_env();
    let client = Client::builder().timeout(Duration::from_secs(6)).build()?;

    println!(
        "[RustSniper] Started | RPCs={} | Executor={} | MinProfit=${}",
        cfg.rpc_urls.len(),
        cfg.executor_address,
        cfg.min_profit_usd
    );

    let mut rpc_idx = 0usize;

    loop {
        let now_ts = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs();
        let raw_targets = fs::read_to_string(&cfg.targets_file).unwrap_or_else(|_| "{}".to_string());
        let targets: TargetsFile = serde_json::from_str(&raw_targets).unwrap_or(TargetsFile {
            updated_at: 0,
            morpho_targets: vec![],
            dutch_targets: vec![],
            harvest_targets: vec![],
        });

        if targets.updated_at > 0 && now_ts.saturating_sub(targets.updated_at) > 300 {
            eprintln!(
                "[RustSniper] WARNING: targets.json is stale (age={}s > 300s). Skipping cycle.",
                now_ts.saturating_sub(targets.updated_at)
            );
            tokio::time::sleep(Duration::from_millis(cfg.poll_interval_ms)).await;
            continue;
        }

        let mut candidates = Vec::new();
        candidates.extend(build_morpho_candidates(&cfg.executor_address, &targets.morpho_targets));
        candidates.extend(build_dutch_candidates(&cfg.executor_address, &targets.dutch_targets));
        candidates.extend(build_harvest_candidates(&cfg.executor_address, &targets.harvest_targets));

        if !candidates.is_empty() {
            let rpc_url = &cfg.rpc_urls[rpc_idx % cfg.rpc_urls.len()];
            rpc_idx = rpc_idx.wrapping_add(1);

            let block_number = get_block_number(&client, rpc_url).await.unwrap_or(0);

            let results = simulate_batch_on_multicall3(&client, rpc_url, &cfg.multicall3_address, &candidates).await.unwrap_or_default();

            let mut verified = Vec::new();
            for (cand, (passed, profit_raw)) in candidates.into_iter().zip(results.into_iter()) {
                let min_req = cand.min_profit_raw.parse::<u128>().unwrap_or(0);
                let scale = 10f64.powi(cand.profit_decimals as i32);
                let profit_usd = (profit_raw as f64 / scale) * cand.token_price_usd;

                if passed && profit_raw >= min_req && profit_usd >= cfg.min_profit_usd {
                    println!(
                        "[RustSniper] VERIFIED PROFITABLE [{}] {} | SimProfit={} (${:.4}) at block {}",
                        cand.pipeline, cand.description, profit_raw, profit_usd, block_number
                    );
                    verified.push(VerifiedOpportunity {
                        pipeline: cand.pipeline,
                        description: cand.description,
                        executor_calldata: cand.calldata_hex,
                        min_profit_raw: cand.min_profit_raw,
                        simulated_profit_raw: profit_raw.to_string(),
                        simulated_profit_usd: profit_usd,
                        profit_token: cand.profit_token,
                        profit_decimals: cand.profit_decimals,
                        token_price_usd: cand.token_price_usd,
                        block_number,
                        timestamp: now_ts,
                    });
                }
            }

            if !verified.is_empty() {
                let out_json = serde_json::to_string_pretty(&verified)?;
                fs::write(&cfg.outbox_file, out_json)?;
            }
        }

        tokio::time::sleep(Duration::from_millis(cfg.poll_interval_ms)).await;
    }
}
