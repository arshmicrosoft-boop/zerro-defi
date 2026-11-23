//! WebSocket `newHeads` subscriber for Base Mainnet.
//!
//! Replaces fixed-interval polling with block-triggered simulation: the moment a
//! new block is announced we wake the sniper loop, removing up to
//! `BLOCK_POLL_MS` of dead time per cycle. If the stream drops we reconnect with
//! backoff, and the caller keeps its HTTP poll as a fallback so the bot never
//! goes blind.

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio_tungstenite::{connect_async, tungstenite::Message};

/// Connect, subscribe to `newHeads`, and forward each block number to `tx`.
///
/// Runs forever (until the receiver is dropped), reconnecting on any failure.
pub async fn run_new_heads(url: String, tx: mpsc::Sender<u64>) {
    let mut backoff_secs: u64 = 1;

    loop {
        match connect_async(&url).await {
            Ok((mut ws, _resp)) => {
                backoff_secs = 1;
                let sub = json!({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_subscribe",
                    "params": ["newHeads"]
                });

                if ws.send(Message::Text(sub.to_string().into())).await.is_err() {
                    continue;
                }

                while let Some(frame) = ws.next().await {
                    match frame {
                        Ok(Message::Text(text)) => {
                            if let Ok(v) = serde_json::from_str::<Value>(text.as_str()) {
                                if let Some(num) = v
                                    .pointer("/params/result/number")
                                    .and_then(|n| n.as_str())
                                {
                                    if let Ok(block) =
                                        u64::from_str_radix(num.trim_start_matches("0x"), 16)
                                    {
                                        // Bounded channel: apply backpressure rather than
                                        // queueing an unbounded pile of blocks.
                                        if tx.send(block).await.is_err() {
                                            return;
                                        }
                                    }
                                }
                            }
                        }
                        Ok(Message::Ping(payload)) => {
                            let _ = ws.send(Message::Pong(payload)).await;
                        }
                        Ok(Message::Close(_)) | Err(_) => break,
                        _ => {}
                    }
                }
            }
            Err(_) => {
                // Connection failed; fall through to backoff.
            }
        }

        tokio::time::sleep(std::time::Duration::from_secs(backoff_secs)).await;
        backoff_secs = (backoff_secs * 2).min(30);
    }
}
