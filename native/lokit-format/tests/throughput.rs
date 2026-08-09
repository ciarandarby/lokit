//! Dependency-free manual throughput benchmark.
//!
//! From the repository root, run:
//! `cargo test --release --manifest-path native/lokit-format/Cargo.toml --test throughput -- --ignored --nocapture`

use std::io::{BufReader, Cursor};
use std::time::{Duration, Instant};

use lokit_format::{format_source, parse_str, BaseStructure, Data, StreamingReader};

const UNIT_COUNT: usize = 100_000;
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

#[test]
#[ignore = "manual release-mode throughput measurement"]
fn hundred_thousand_unit_throughput() {
    let mut document = BaseStructure::new("en-US");
    document.target_locale = Some("fr-FR".to_owned());
    document.data.reserve(UNIT_COUNT);
    for index in 0..UNIT_COUNT {
        let mut data = Data::new(format!("Source message {index}"));
        data.target = Some(format!("Message cible {index}"));
        document.data.push((format!("unit-{index}"), data));
    }
    let expected_checksum = checksum(document.data.iter().map(|(id, data)| (id.as_str(), data)));

    let write_started = Instant::now();
    let source = format_source(&document).expect("benchmark document should format");
    let write_elapsed = write_started.elapsed();

    let stream_started = Instant::now();
    let mut reader = StreamingReader::new(BufReader::with_capacity(
        64 * 1024,
        Cursor::new(source.as_bytes()),
    ))
    .expect("benchmark stream should open");
    let mut streamed_count = 0_usize;
    let mut streamed_checksum = FNV_OFFSET;
    while let Some((unit_id, data)) = reader.next_unit().expect("streamed unit should parse") {
        streamed_checksum = checksum_one(streamed_checksum, &unit_id, &data);
        streamed_count += 1;
    }
    let stream_elapsed = stream_started.elapsed();

    let parse_started = Instant::now();
    let parsed = parse_str(&source).expect("benchmark document should parse");
    let parse_elapsed = parse_started.elapsed();
    let parsed_checksum = checksum(parsed.data.iter().map(|(id, data)| (id.as_str(), data)));

    assert_eq!(streamed_count, UNIT_COUNT);
    assert_eq!(streamed_checksum, expected_checksum);
    assert_eq!(parsed_checksum, expected_checksum);
    assert_eq!(parsed, document);
    assert_eq!(
        format_source(&parsed).expect("parsed benchmark document should reformat"),
        source
    );

    println!(
        "units={UNIT_COUNT} bytes={} checksum={expected_checksum:016x}",
        source.len()
    );
    print_result("canonical write", source.len(), write_elapsed);
    print_result("stream parse", source.len(), stream_elapsed);
    print_result("materialized parse", source.len(), parse_elapsed);
}

fn checksum<'a>(units: impl Iterator<Item = (&'a str, &'a Data)>) -> u64 {
    units.fold(FNV_OFFSET, |value, (unit_id, data)| {
        checksum_one(value, unit_id, data)
    })
}

fn checksum_one(mut value: u64, unit_id: &str, data: &Data) -> u64 {
    value = checksum_bytes(value, unit_id.as_bytes());
    value = checksum_bytes(value, &[0]);
    value = checksum_bytes(value, data.source.as_bytes());
    value = checksum_bytes(value, &[0]);
    if let Some(target) = &data.target {
        value = checksum_bytes(value, target.as_bytes());
    }
    checksum_bytes(value, &[0xff])
}

fn checksum_bytes(mut value: u64, bytes: &[u8]) -> u64 {
    for byte in bytes {
        value ^= u64::from(*byte);
        value = value.wrapping_mul(FNV_PRIME);
    }
    value
}

fn print_result(label: &str, bytes: usize, elapsed: Duration) {
    let mebibytes = bytes as f64 / (1024.0 * 1024.0);
    let throughput = mebibytes / elapsed.as_secs_f64();
    println!("{label}: {elapsed:.3?}, {throughput:.1} MiB/s");
}
