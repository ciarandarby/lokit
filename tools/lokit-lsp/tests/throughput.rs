//! Reproducible release-mode LSP throughput and edit-storm benchmark.
//!
//! From the repository root, run:
//! `cargo test --release --locked --manifest-path tools/lokit-lsp/Cargo.toml --test throughput -- --ignored --nocapture`

use std::error::Error;
use std::io;
use std::time::{Duration, Instant};

use futures::StreamExt;
use lokit_format::{BaseStructure, Data, format_source, parse_str};
use lokit_lsp::Backend;
use serde_json::{Value, json};
use tower::{Service, ServiceExt};
use tower_lsp_server::jsonrpc::Request;
use tower_lsp_server::{ClientSocket, LspService};

const LARGE_UNIT_COUNT: usize = 50_000;
const LARGE_PAYLOAD_CHARACTERS: usize = 320;
const STORM_EDITS: i32 = 10_000;
const NOTIFICATION_TIMEOUT: Duration = Duration::from_secs(30);
const MIN_ANALYSIS_MIB_PER_SECOND: f64 = 8.0;
const MIN_FORMAT_MIB_PER_SECOND: f64 = 15.0;
const MIN_EDIT_REQUESTS_PER_SECOND: f64 = 10_000.0;
const VALID_STORM_DOCUMENT: &str =
    "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n}\n";

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "manual release-mode LSP throughput and stress measurement"]
#[allow(clippy::too_many_lines)]
async fn protocol_throughput_and_latest_only_edit_storm() -> Result<(), Box<dyn Error + Send + Sync>>
{
    if cfg!(debug_assertions) {
        return Err(io::Error::other("throughput benchmark must run with --release").into());
    }

    let source = large_fixture()?;
    let source_bytes = source.len();
    let parsed = parse_str(&source)?;
    assert_eq!(parsed.data.len(), LARGE_UNIT_COUNT);
    assert_eq!(format_source(&parsed)?, source);

    let (mut service, mut socket) = LspService::new(Backend::new);
    service
        .ready()
        .await?
        .call(
            Request::build("initialize")
                .params(json!({
                    "capabilities": {
                        "textDocument": {
                            "publishDiagnostics": {"versionSupport": true}
                        }
                    }
                }))
                .id(1)
                .finish(),
        )
        .await?;
    service
        .ready()
        .await?
        .call(Request::build("initialized").params(json!({})).finish())
        .await?;

    let analysis_started = Instant::now();
    service
        .ready()
        .await?
        .call(
            Request::build("textDocument/didOpen")
                .params(json!({
                    "textDocument": {
                        "uri": "file:///benchmark/large.lokit",
                        "languageId": "lokit",
                        "version": 1,
                        "text": source
                    }
                }))
                .finish(),
        )
        .await?;
    let analysis_diagnostic = next_notification(&mut socket).await?;
    let analysis_elapsed = analysis_started.elapsed();
    assert_eq!(
        analysis_diagnostic["method"],
        "textDocument/publishDiagnostics"
    );
    assert_eq!(analysis_diagnostic["params"]["version"], 1);
    assert_eq!(analysis_diagnostic["params"]["diagnostics"], json!([]));

    let format_started = Instant::now();
    let format_response = service
        .ready()
        .await?
        .call(
            Request::build("textDocument/formatting")
                .params(json!({
                    "textDocument": {"uri": "file:///benchmark/large.lokit"},
                    "options": {"tabSize": 2, "insertSpaces": true}
                }))
                .id(2)
                .finish(),
        )
        .await?;
    let format_elapsed = format_started.elapsed();
    let format_response = serde_json::to_value(format_response)?;
    assert_eq!(format_response["result"], json!([]));

    let analysis_rate = mebibytes_per_second(source_bytes, analysis_elapsed)?;
    let format_rate = mebibytes_per_second(source_bytes, format_elapsed)?;
    println!(
        "large_document: units={LARGE_UNIT_COUNT} bytes={source_bytes} analysis={analysis_elapsed:.3?} ({analysis_rate:.1} MiB/s) format={format_elapsed:.3?} ({format_rate:.1} MiB/s)"
    );
    assert!(analysis_rate >= MIN_ANALYSIS_MIB_PER_SECOND);
    assert!(format_rate >= MIN_FORMAT_MIB_PER_SECOND);

    service
        .ready()
        .await?
        .call(
            Request::build("textDocument/didOpen")
                .params(json!({
                    "textDocument": {
                        "uri": "file:///benchmark/storm.lokit",
                        "languageId": "lokit",
                        "version": 1,
                        "text": VALID_STORM_DOCUMENT
                    }
                }))
                .finish(),
        )
        .await?;
    let storm_open = next_notification(&mut socket).await?;
    assert_eq!(storm_open["params"]["version"], 1);
    assert_eq!(storm_open["params"]["diagnostics"], json!([]));

    let storm_started = Instant::now();
    for edit in 1..=STORM_EDITS {
        let text = if edit == STORM_EDITS {
            "not a lokit document\n"
        } else {
            VALID_STORM_DOCUMENT
        };
        service
            .ready()
            .await?
            .call(
                Request::build("textDocument/didChange")
                    .params(json!({
                        "textDocument": {
                            "uri": "file:///benchmark/storm.lokit",
                            "version": edit + 1
                        },
                        "contentChanges": [{"text": text}]
                    }))
                    .finish(),
            )
            .await?;
    }
    let ingestion_elapsed = storm_started.elapsed();
    let final_version = i64::from(STORM_EDITS + 1);
    let mut publication_count = 0_usize;
    let mut last_version = 0_i64;
    loop {
        let diagnostic = next_notification(&mut socket).await?;
        if diagnostic["params"]["uri"] != "file:///benchmark/storm.lokit" {
            continue;
        }
        let version = diagnostic["params"]["version"]
            .as_i64()
            .ok_or_else(|| io::Error::other("storm diagnostic omitted its version"))?;
        assert!(version > last_version);
        last_version = version;
        publication_count += 1;
        if version == final_version {
            assert_eq!(diagnostic["params"]["diagnostics"][0]["code"], "LKT005");
            break;
        }
    }
    let storm_elapsed = storm_started.elapsed();
    let edit_rate = f64::from(STORM_EDITS) / ingestion_elapsed.as_secs_f64();
    assert!(edit_rate >= MIN_EDIT_REQUESTS_PER_SECOND);
    assert!(publication_count < usize::try_from(STORM_EDITS)? / 10);

    println!(
        "edit_storm: edits={STORM_EDITS} ingest={ingestion_elapsed:.3?} ({edit_rate:.0} edits/s) final_diagnostic={storm_elapsed:.3?} publications={publication_count}"
    );
    Ok(())
}

fn large_fixture() -> Result<String, Box<dyn Error + Send + Sync>> {
    let mut document = BaseStructure::new("en-US");
    document.target_locale = Some("fr-FR".to_owned());
    document.data.reserve(LARGE_UNIT_COUNT);
    let payload = "x".repeat(LARGE_PAYLOAD_CHARACTERS);
    for index in 0..LARGE_UNIT_COUNT {
        let mut data = Data::new(format!("Source message {index} {payload}"));
        data.target = Some(format!("Message cible {index} {payload}"));
        document.data.push((format!("unit-{index}"), data));
    }
    Ok(format_source(&document)?)
}

async fn next_notification(
    socket: &mut ClientSocket,
) -> Result<Value, Box<dyn Error + Send + Sync>> {
    let notification = tokio::time::timeout(NOTIFICATION_TIMEOUT, socket.next())
        .await?
        .ok_or_else(|| io::Error::other("LSP notification channel closed"))?;
    Ok(serde_json::to_value(notification)?)
}

fn mebibytes_per_second(
    bytes: usize,
    elapsed: Duration,
) -> Result<f64, Box<dyn Error + Send + Sync>> {
    let bytes = u32::try_from(bytes)?;
    Ok(f64::from(bytes) / (1024.0 * 1024.0) / elapsed.as_secs_f64())
}
