use crate::types::CandidateCall;
use alloy_primitives::{keccak256, Address, Bytes, U256};
use alloy_sol_types::{sol, SolCall, SolValue};
use anyhow::{anyhow, Result};
use reqwest::Client;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::str::FromStr;

sol! {
    struct Call3 {
        address target;
        bytes callData;
    }

    struct Result3 {
        bool success;
        bytes returnData;
    }

    function tryAggregate(bool requireSuccess, Call3[] calldata calls)
        external
        payable
        returns (Result3[] memory returnData);
}

fn build_executor_state_override(
    executor_address: &str,
    candidates: &[CandidateCall],
) -> Option<Value> {
    let artifact_paths = [
        "../contracts/out/ZeroCapOmniExecutor.json",
        "contracts/out/ZeroCapOmniExecutor.json",
    ];
    let mut deployed_bytecode = String::new();
    for p in artifact_paths {
        if Path::new(p).exists() {
            if let Ok(raw) = fs::read_to_string(p) {
                if let Ok(v) = serde_json::from_str::<Value>(&raw) {
                    if let Some(code) = v.get("deployedBytecode").and_then(|c| c.as_str()) {
                        deployed_bytecode = code.to_string();
                        break;
                    }
                }
            }
        }
    }
    if deployed_bytecode.len() <= 2 {
        return None;
    }

    let mut state_diff = HashMap::new();
    state_diff.insert(
        "0x0000000000000000000000000000000000000000000000000000000000000000".to_string(),
        "0x0000000000000000000000001111111111111111111111111111111111111111".to_string(),
    );

    for c in candidates {
        let raw_hex = c.calldata_hex.trim_start_matches("0x");
        if raw_hex.len() >= 72 {
            let first_arg_word = &raw_hex[8..72];
            if let Ok(arg_bytes) = hex::decode(first_arg_word) {
                let mut key_preimage = Vec::with_capacity(64);
                key_preimage.extend_from_slice(&arg_bytes);
                let slot_two = U256::from(2u64).to_be_bytes::<32>();
                key_preimage.extend_from_slice(&slot_two);
                let slot_hash = keccak256(&key_preimage);
                state_diff.insert(
                    format!("0x{}", hex::encode(slot_hash)),
                    "0x0000000000000000000000000000000000000000000000000000000000000001".to_string(),
                );
            }
        }
    }

    Some(json!({
        executor_address: {
            "code": deployed_bytecode,
            "stateDiff": state_diff
        }
    }))
}

pub fn encode_try_aggregate(candidates: &[CandidateCall]) -> Result<String> {
    let mut calls = Vec::with_capacity(candidates.len());
    for c in candidates {
        let target = Address::from_str(&c.target_executor)?;
        let raw_bytes = hex::decode(c.calldata_hex.trim_start_matches("0x"))?;
        calls.push(Call3 {
            target,
            callData: Bytes::from(raw_bytes),
        });
    }
    let call = tryAggregateCall {
        requireSuccess: false,
        calls,
    };
    Ok(format!("0x{}", hex::encode(call.abi_encode())))
}

pub fn decode_try_aggregate_results(hex_response: &str) -> Result<Vec<(bool, u128)>> {
    let clean = hex_response.trim_start_matches("0x");
    if clean.is_empty() {
        return Ok(vec![]);
    }
    let bytes = hex::decode(clean)?;
    let decoded: Vec<Result3> = <Vec<Result3> as SolValue>::abi_decode(&bytes, false)?;
    let mut out = Vec::with_capacity(decoded.len());
    for r in decoded {
        let mut profit: u128 = 0;
        if r.success && r.returnData.len() >= 32 {
            let word = U256::from_be_slice(&r.returnData[0..32]);
            profit = word.try_into().unwrap_or(u128::MAX);
        }
        out.push((r.success && r.returnData.len() >= 32, profit));
    }
    Ok(out)
}

pub async fn simulate_batch_on_multicall3(
    client: &Client,
    rpc_url: &str,
    multicall3_address: &str,
    candidates: &[CandidateCall],
) -> Result<Vec<(bool, u128)>> {
    if candidates.is_empty() {
        return Ok(vec![]);
    }

    let calldata_hex = encode_try_aggregate(candidates)?;
    let executor_addr = &candidates[0].target_executor;

    let params = if let Some(override_obj) = build_executor_state_override(executor_addr, candidates) {
        json!([
            {
                "to": multicall3_address,
                "data": calldata_hex,
                "gas": "0x2faf080"
            },
            "latest",
            override_obj
        ])
    } else {
        json!([
            {
                "to": multicall3_address,
                "data": calldata_hex,
                "gas": "0x2faf080"
            },
            "latest"
        ])
    };

    let payload = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": params
    });

    let resp: Value = client.post(rpc_url).json(&payload).send().await?.json().await?;

    if let Some(err) = resp.get("error") {
        return Err(anyhow!("RPC eth_call error: {}", err));
    }

    let result_hex = resp
        .get("result")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("Missing result in eth_call response"))?;

    decode_try_aggregate_results(result_hex)
}

pub async fn get_block_number(client: &Client, rpc_url: &str) -> Result<u64> {
    let payload = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_blockNumber",
        "params": []
    });
    let resp: Value = client.post(rpc_url).json(&payload).send().await?.json().await?;
    let hex_str = resp
        .get("result")
        .and_then(|v| v.as_str())
        .unwrap_or("0x0");
    Ok(u64::from_str_radix(hex_str.trim_start_matches("0x"), 16).unwrap_or(0))
}
