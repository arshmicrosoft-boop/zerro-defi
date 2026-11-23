mod config;
mod pipelines;
mod sim;
mod types;
mod ui;
mod ws;

use config::SniperConfig;
use pipelines::{
    dutch::build_dutch_candidates, harvest::build_harvest_candidates,
    morpho::build_morpho_candidates,
};
use reqwest::Client;
use sim::{get_block_number, simulate_batch_on_multicall3};
use std::collections::{HashMap, VecDeque};
use std::fs;
use std::io::Write;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use types::{TargetsFile, VerifiedOpportunity};
use ui::{
    glyph, paint, Frame, Screen, ACCENT, AMBER, FAINT, GREEN, INK, MUTED, RED, SKY, VIOLET,
};

const TARGET_TTL_SEC: u64 = 300;

struct Stats {
    cycles: u64,
    sims: u64,
    verified: u64,
    errors: u64,
    best_usd: f64,
    block: u64,
    latency_ms: u128,
    cycle_ms: u128,
    targets_age: u64,
    morpho: usize,
    dutch: usize,
    harvest: usize,
    candidates: usize,
    cursor: usize,
    stale: bool,
    stale_logged: bool,
    targets_missing: bool,
    executor_onchain: bool,
    rpc_host: String,
    ws_enabled: bool,
    ws_blocks: u64,
    last_error: String,
    events: VecDeque<(u64, &'static str, String)>,
}

impl Stats {
    fn new(cfg: &SniperConfig) -> Self {
        Stats {
            cycles: 0,
            sims: 0,
            verified: 0,
            errors: 0,
            best_usd: 0.0,
            block: 0,
            latency_ms: 0,
            cycle_ms: 0,
            targets_age: 0,
            morpho: 0,
            dutch: 0,
            harvest: 0,
            candidates: 0,
            cursor: 0,
            stale: false,
            stale_logged: false,
            targets_missing: false,
            executor_onchain: !cfg.executor_address.eq_ignore_ascii_case("0x0000000000000000000000000000000000000000"),
            rpc_host: host_of(cfg.rpc_urls.first().map(|s| s.as_str()).unwrap_or("")),
            ws_enabled: cfg.ws_url.is_some(),
            ws_blocks: 0,
            last_error: String::new(),
            events: VecDeque::with_capacity(64),
        }
    }

    fn event(&mut self, level: &'static str, message: String) {
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        self.events.push_back((ts, level, message));
        while self.events.len() > 32 {
            self.events.pop_front();
        }
    }
}

fn host_of(url: &str) -> String {
    url.split("//")
        .nth(1)
        .unwrap_or(url)
        .split('/')
        .next()
        .unwrap_or(url)
        .to_string()
}

fn clock_of(ts: u64) -> String {
    let secs = ts % 86_400;
    format!("{:02}:{:02}:{:02}", secs / 3600, (secs % 3600) / 60, secs % 60)
}

fn level_color(level: &str) -> &'static str {
    match level {
        "ok" => GREEN,
        "warn" => AMBER,
        "error" => RED,
        "trade" => ACCENT,
        _ => MUTED,
    }
}

fn level_mark(level: &str) -> &'static str {
    match level {
        "ok" => glyph("check"),
        "error" => glyph("cross"),
        "trade" => glyph("bolt"),
        "warn" => "!",
        _ => "·",
    }
}

fn render(stats: &Stats, cfg: &SniperConfig, started: Instant) -> Vec<String> {
    let (cols, _) = ui::term_size();
    let width = (cols.saturating_sub(1)).clamp(78, 116);
    let uptime = started.elapsed().as_secs();

    let mut frame = Frame::new(
        width,
        "ZERRO-DEFI  ·  SNIPER",
        &format!("{} LIVE  {} UTC", glyph("dot"), ui::utc_clock()),
    );

    frame.pair(
        &format!(
            "{}  {}  {} {}  {} {}",
            paint("BASE", ACCENT, true),
            paint("8453", FAINT, false),
            paint("· block", MUTED, false),
            paint(&ui::human_int(stats.block), INK, true),
            paint("· rpc", MUTED, false),
            paint(&stats.rpc_host, SKY, false),
        ),
        &format!(
            "{}{}",
            paint(&format!("{}ms", stats.latency_ms), MUTED, false),
            paint(&format!("  ·  up {}", ui::human_secs(uptime)), FAINT, false),
        ),
    );

    let exec_mode = if stats.executor_onchain {
        paint("on-chain", GREEN, true)
    } else {
        paint("stateOverride", AMBER, true)
    };
    let ws_chip = if stats.ws_enabled {
        format!(
            "  {} {}",
            paint("· ws", FAINT, false),
            paint(&format!("{}", stats.ws_blocks), MUTED, false)
        )
    } else {
        String::new()
    };
    frame.pair(
        &format!(
            "{} {}  {} {}  {} {}",
            paint("executor", MUTED, false),
            paint(&ui::short_addr(&cfg.executor_address, 8, 6), SKY, false),
            paint("· mode", MUTED, false),
            exec_mode,
            paint("· min", MUTED, false),
            paint(&ui::usd(cfg.min_profit_usd), INK, false),
        ),
        &format!(
            "{}{}{}",
            paint("poll ", FAINT, false),
            paint(&format!("{}ms", cfg.poll_interval_ms), FAINT, false),
            ws_chip,
        ),
    );

    frame.blank();
    frame.rule("CYCLE");
    let stale_chip = if stats.stale {
        paint("[ STALE ]", AMBER, true)
    } else {
        paint("[ FRESH ]", GREEN, true)
    };
    frame.pair(
        &format!(
            "{} {}  {} {}  {} {}  {} {}",
            paint("targets", MUTED, false),
            paint(&format!("{}", stats.morpho + stats.dutch + stats.harvest), INK, true),
            paint("age", MUTED, false),
            paint(&ui::human_secs(stats.targets_age), MUTED, false),
            paint("candidates", MUTED, false),
            paint(
                &format!(
                    "{}/{}",
                    stats.candidates,
                    stats.morpho + stats.dutch + stats.harvest
                ),
                INK,
                true,
            ),
            paint("cycle", MUTED, false),
            paint(&format!("{}ms", stats.cycle_ms), MUTED, false),
        ),
        &stale_chip,
    );

    let hit_rate = if stats.sims > 0 {
        stats.verified as f64 / stats.sims as f64
    } else {
        0.0
    };
    frame.pair(
        &format!(
            "{} {}  {} {}  {} {}  {} {}",
            paint("sims", MUTED, false),
            paint(&format!("{}", stats.sims), INK, true),
            paint("verified", MUTED, false),
            paint(&format!("{}", stats.verified), if stats.verified > 0 { GREEN } else { FAINT }, true),
            paint("best", MUTED, false),
            paint(&ui::usd(stats.best_usd), if stats.best_usd > 0.0 { GREEN } else { FAINT }, true),
            paint("errors", MUTED, false),
            paint(&format!("{}", stats.errors), if stats.errors > 0 { RED } else { FAINT }, true),
        ),
        &format!(
            "{} {}",
            paint("hit", FAINT, false),
            paint(&format!("{:.1}%", hit_rate * 100.0), FAINT, false),
        ),
    );

    frame.rule("PIPELINE");
    let max_count = stats.morpho.max(stats.dutch).max(stats.harvest).max(1);
    frame.pair(
        &format!(
            "{} {}  {} {}  {} {}",
            paint("morpho", VIOLET, false),
            paint(&format!("{}", stats.morpho), INK, true),
            paint("dutch", SKY, false),
            paint(&format!("{}", stats.dutch), INK, true),
            paint("harvest", ACCENT, false),
            paint(&format!("{}", stats.harvest), INK, true),
        ),
        &format!(
            "{} {}",
            paint("largest", FAINT, false),
            paint(&ui::human_int(max_count as u64), FAINT, false),
        ),
    );
    frame.row(&format!(
        "{} {}  {} {}  {} {}",
        paint("morpho ", VIOLET, false),
        ui::meter(stats.morpho as f64 / max_count as f64, 14, VIOLET),
        paint("dutch ", SKY, false),
        ui::meter(stats.dutch as f64 / max_count as f64, 14, SKY),
        paint("harvest ", ACCENT, false),
        ui::meter(stats.harvest as f64 / max_count as f64, 14, ACCENT),
    ));

    frame.rule("EVENTS");
    if stats.events.is_empty() {
        frame.row(&paint("waiting for first cycle…", FAINT, false));
    } else {
        let tail: Vec<_> = stats.events.iter().rev().take(6).cloned().collect();
        for (ts, level, message) in tail.into_iter().rev() {
            let head = format!(
                "{} {} {}",
                paint(&clock_of(ts), FAINT, false),
                paint(level_mark(level), level_color(level), false),
                paint("sniper     ", MUTED, false),
            );
            let budget = width.saturating_sub(4).saturating_sub(ui::vlen(&head));
            frame.row(&format!("{} {}", head, paint(&ui::truncate(&message, budget), INK, false)));
        }
    }

    if !stats.last_error.is_empty() {
        frame.rule("LAST ERROR");
        frame.row(&paint(&ui::truncate(&stats.last_error, width - 6), RED, false));
    }

    frame.close()
}

fn write_status(cfg: &SniperConfig, stats: &Stats, started: Instant) {
    let payload = serde_json::json!({
        "updated_at": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0),
        "block": stats.block,
        "candidates": stats.candidates,
        "simulated": stats.sims,
        "verified": stats.verified,
        "best_profit_usd": stats.best_usd,
        "latency_ms": stats.latency_ms,
        "rpc": stats.rpc_host,
        "stale": stats.stale,
        "ws_enabled": stats.ws_enabled,
        "ws_blocks": stats.ws_blocks,
        "error": stats.last_error,
        "cycles": stats.cycles,
        "morpho": stats.morpho,
        "dutch": stats.dutch,
        "harvest": stats.harvest,
        "uptime_sec": started.elapsed().as_secs(),
        "pid": std::process::id(),
    });
    let path = std::path::Path::new(&cfg.status_file);
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(text) = serde_json::to_string(&payload) {
        let _ = fs::write(path, text);
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cfg = SniperConfig::from_env();
    let client = Client::builder().timeout(Duration::from_secs(6)).build()?;
    let started = Instant::now();
    let mut stats = Stats::new(&cfg);
    stats.event("info", format!("sniper online · {} rpc endpoint(s)", cfg.rpc_urls.len()));

    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            print!("\x1b[?25h\x1b[?1049l");
            let _ = std::io::stdout().flush();
            std::process::exit(0);
        }
    });

    let mut screen = Screen::new();
    screen.enter();
    let mut rpc_idx = 0usize;

    // Block-triggered simulation: wake on every new head, with the HTTP poll as
    // a fallback so the loop still runs if the WebSocket is unavailable.
    let mut ws_rx: Option<tokio::sync::mpsc::Receiver<u64>> = None;
    if let Some(url) = cfg.ws_url.clone() {
        let (tx, rx) = tokio::sync::mpsc::channel::<u64>(16);
        let ws_url = url.clone();
        tokio::spawn(async move { ws::run_new_heads(ws_url, tx).await });
        stats.event("info", format!("newHeads ws armed · {}", host_of(&url)));
        ws_rx = Some(rx);
    }

    loop {
        let cycle_start = Instant::now();
        let now_ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);

        let raw_targets = match fs::read_to_string(&cfg.targets_file) {
            Ok(text) => {
                stats.targets_missing = false;
                text
            }
            Err(err) => {
                if !stats.targets_missing {
                    stats.event(
                        "error",
                        format!("targets unreadable · {} · {}", cfg.targets_file, err),
                    );
                    stats.targets_missing = true;
                }
                "{}".to_string()
            }
        };
        let targets: TargetsFile = match serde_json::from_str(&raw_targets) {
            Ok(parsed) => parsed,
            Err(err) => {
                if !stats.targets_missing {
                    stats.event("error", format!("targets parse failed · {}", err));
                    stats.targets_missing = true;
                }
                TargetsFile {
                    updated_at: 0,
                    morpho_targets: vec![],
                    dutch_targets: vec![],
                    harvest_targets: vec![],
                }
            }
        };

        stats.morpho = targets.morpho_targets.len();
        stats.dutch = targets.dutch_targets.len();
        stats.harvest = targets.harvest_targets.len();
        stats.targets_age = if targets.updated_at > 0 {
            now_ts.saturating_sub(targets.updated_at)
        } else {
            0
        };

        if targets.updated_at > 0 && stats.targets_age > TARGET_TTL_SEC {
            stats.stale = true;
            if !stats.stale_logged {
                stats.event(
                    "warn",
                    format!("targets stale ({} > {}s) · holding fire", stats.targets_age, TARGET_TTL_SEC),
                );
                stats.stale_logged = true;
            }
            stats.candidates = 0;
        } else {
            stats.stale = false;
            if stats.stale_logged {
                stats.event("ok", "targets refreshed · resuming".to_string());
                stats.stale_logged = false;
            }

            let mut candidates = Vec::new();
            candidates.extend(build_morpho_candidates(&cfg.executor_address, &targets.morpho_targets));
            candidates.extend(build_dutch_candidates(&cfg.executor_address, &targets.dutch_targets));
            candidates.extend(build_harvest_candidates(&cfg.executor_address, &targets.harvest_targets));
            stats.candidates = candidates.len();

            if !candidates.is_empty() {
                let rpc_url = cfg.rpc_urls[rpc_idx % cfg.rpc_urls.len()].clone();
                rpc_idx = rpc_idx.wrapping_add(1);
                stats.rpc_host = host_of(&rpc_url);

                let block_number = get_block_number(&client, &rpc_url).await.unwrap_or(0);
                stats.block = block_number;

                let sim_started = Instant::now();
                let batch_size = std::env::var("SIM_BATCH_SIZE")
                    .ok()
                    .and_then(|v| v.parse::<usize>().ok())
                    .unwrap_or(6)
                    .max(1);
                let max_per_cycle = std::env::var("MAX_SIM_PER_CYCLE")
                    .ok()
                    .and_then(|v| v.parse::<usize>().ok())
                    .unwrap_or(24)
                    .max(1);

                // Rotate through the target list so every cycle stays inside RPC gas caps.
                if candidates.len() > max_per_cycle {
                    let start = stats.cursor % candidates.len();
                    let mut window = Vec::with_capacity(max_per_cycle);
                    for offset in 0..max_per_cycle {
                        window.push(candidates[(start + offset) % candidates.len()].clone());
                    }
                    stats.cursor = (start + max_per_cycle) % candidates.len();
                    candidates = window;
                }
                stats.candidates = candidates.len();

                let mut results: Vec<(bool, u128)> = Vec::with_capacity(candidates.len());
                let mut failed_batches = 0usize;
                let mut successes = 0usize;
                let total_batches = (candidates.len() + batch_size - 1) / batch_size;

                for chunk in candidates.chunks(batch_size) {
                    match simulate_batch_on_multicall3(
                        &client,
                        &rpc_url,
                        &cfg.multicall3_address,
                        chunk,
                    )
                    .await
                    {
                        Ok(mut batch) => {
                            successes += batch.len();
                            results.append(&mut batch);
                        }
                        Err(err) => {
                            failed_batches += 1;
                            stats.errors += 1;
                            stats.last_error = err.to_string();
                            results.extend(std::iter::repeat((false, 0u128)).take(chunk.len()));
                        }
                    }
                }

                stats.latency_ms = sim_started.elapsed().as_millis();
                let result_count = successes;
                stats.sims += successes as u64;

                if failed_batches == 0 {
                    stats.last_error.clear();
                } else {
                    let err_snapshot = stats.last_error.clone();
                    stats.event(
                        "error",
                        format!(
                            "{} of {} sim batch(es) failed · {}",
                            failed_batches, total_batches, err_snapshot
                        ),
                    );
                }

                let mut verified = Vec::new();
                for (cand, (passed, profit_raw)) in
                    candidates.into_iter().zip(results.into_iter())
                {
                    let min_req = cand.min_profit_raw.parse::<u128>().unwrap_or(0);
                    let scale = 10f64.powi(cand.profit_decimals as i32);
                    let profit_usd = (profit_raw as f64 / scale) * cand.token_price_usd;

                    if passed && profit_raw >= min_req && profit_usd >= cfg.min_profit_usd {
                        stats.verified += 1;
                        if profit_usd > stats.best_usd {
                            stats.best_usd = profit_usd;
                        }
                        stats.event(
                            "trade",
                            format!(
                                "{} · {} · {}",
                                cand.pipeline,
                                cand.description,
                                ui::usd(profit_usd)
                            ),
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
                    // Collapse the 25/50/100% tier ladder (and any duplicate
                    // target) to the single highest-profit opportunity per
                    // (pipeline, description). Submitting two tiers for the same
                    // borrower would make the second revert once the first
                    // repairs the position.
                    if verified.len() > 1 {
                        let mut best: HashMap<(String, String), VerifiedOpportunity> =
                            HashMap::new();
                        for v in verified.into_iter() {
                            let key = (v.pipeline.clone(), v.description.clone());
                            let replace = match best.get(&key) {
                                Some(existing) => {
                                    let a = v.simulated_profit_raw.parse::<u128>().unwrap_or(0);
                                    let b =
                                        existing.simulated_profit_raw.parse::<u128>().unwrap_or(0);
                                    a > b
                                }
                                None => true,
                            };
                            if replace {
                                best.insert(key, v);
                            }
                        }
                        verified = best.into_values().collect();
                        verified.sort_by(|a, b| {
                            let ap = a.simulated_profit_raw.parse::<u128>().unwrap_or(0);
                            let bp = b.simulated_profit_raw.parse::<u128>().unwrap_or(0);
                            bp.cmp(&ap)
                        });
                    }

                    if let Ok(out_json) = serde_json::to_string_pretty(&verified) {
                        if fs::write(&cfg.outbox_file, out_json).is_ok() {
                            stats.event(
                                "ok",
                                format!("{} verified op(s) → outbox", verified.len()),
                            );
                        }
                    }
                } else if stats.cycles % 20 == 0 {
                    stats.event(
                        "info",
                        format!("{} calls simulated · no profitable edge", result_count),
                    );
                }
            }
        }

        stats.cycles += 1;
        stats.cycle_ms = cycle_start.elapsed().as_millis();
        write_status(&cfg, &stats, started);
        screen.draw(&render(&stats, &cfg, started));

        match ws_rx.as_mut() {
            Some(rx) => {
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_millis(cfg.poll_interval_ms)) => {}
                    maybe = rx.recv() => {
                        if maybe.is_some() {
                            stats.ws_blocks += 1;
                        }
                    }
                }
            }
            None => tokio::time::sleep(Duration::from_millis(cfg.poll_interval_ms)).await,
        }
    }
}
