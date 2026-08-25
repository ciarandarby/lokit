//! Dependency-free manual throughput benchmark.
//!
//! From the repository root, run:
//! `cargo test --release --manifest-path native/lokit-format/Cargo.toml --test throughput -- --ignored --nocapture`

use std::io::{BufReader, Cursor};
use std::time::{Duration, Instant};

use lokit_format::id_registry::BoundedIdRegistry;
use lokit_format::{
    canonicalize_placeholders, format_source, parse_str, project_segment_placeholders,
    reform_placeholders, resolve_data_placeholders, resolve_segment_placeholders, BaseStructure,
    Data, DetectionOptions, PlaceholderProjectionOptions, PlaceholderSyntax, StreamingReader, Tags,
};

const UNIT_COUNT: usize = 100_000;
const PLACEHOLDER_COUNT: usize = 20_000;
const REGISTRY_ID_COUNT: usize = 1_000_000;
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

#[test]
#[ignore = "manual release-mode placeholder throughput measurement"]
fn placeholder_projection_and_reformation_throughput() {
    let mut candidate = String::with_capacity(PLACEHOLDER_COUNT * 28);
    let mut query = String::with_capacity(PLACEHOLDER_COUNT * 28);
    for index in 0..PLACEHOLDER_COUNT {
        candidate.push_str("Value ");
        candidate.push('{');
        candidate.push_str("field_");
        candidate.push_str(&index.to_string());
        candidate.push_str("}; ");

        query.push_str("Value ");
        query.push('{');
        query.push_str("query_");
        query.push_str(&index.to_string());
        query.push_str("}; ");
    }
    let mut detection = DetectionOptions::explicit([PlaceholderSyntax::PythonBrace]);
    detection.limits.max_occurrences = PLACEHOLDER_COUNT;
    let projection_options = PlaceholderProjectionOptions {
        detection: detection.clone(),
        runtime_placeholders: true,
        inline_placeholders: true,
        project_targets: false,
    };

    let project_started = Instant::now();
    let projected = project_segment_placeholders(&candidate, &[], &[], &projection_options)
        .expect("benchmark placeholders should project");
    let project_elapsed = project_started.elapsed();

    let resolve_started = Instant::now();
    let resolved =
        resolve_segment_placeholders(&projected.text, &projected.parts, &projected.tag_map)
            .expect("benchmark projection should resolve");
    let resolve_elapsed = resolve_started.elapsed();

    let mut rebound_data = Data::new(projected.text.clone());
    rebound_data.target = Some(projected.text.clone());
    rebound_data.tags = Some(Tags {
        source_tag_map: projected.tag_map.clone(),
        source_parts: projected.parts.clone(),
        ..Tags::default()
    });
    let rebound_started = Instant::now();
    let rebound = resolve_data_placeholders(rebound_data)
        .expect("benchmark translated target markers should rebind");
    let rebound_elapsed = rebound_started.elapsed();

    let canonical_started = Instant::now();
    let canonical = canonicalize_placeholders(&candidate, &detection)
        .expect("benchmark placeholders should canonicalize");
    let canonical_elapsed = canonical_started.elapsed();

    let reform_started = Instant::now();
    let reformed = reform_placeholders(&candidate, &candidate, &query, &detection)
        .expect("benchmark placeholders should reform");
    let reform_elapsed = reform_started.elapsed();

    assert_eq!(projected.tag_map.len(), PLACEHOLDER_COUNT);
    assert_eq!(resolved.text, candidate);
    assert!(resolved.tag_map.is_empty());
    assert_eq!(rebound.source, candidate);
    assert_eq!(rebound.target.as_deref(), Some(candidate.as_str()));
    assert_eq!(
        canonical.signature.matches(';').count() + 1,
        PLACEHOLDER_COUNT
    );
    assert_eq!(reformed.text, query);
    println!(
        "placeholders={PLACEHOLDER_COUNT} bytes={} projected_bytes={}",
        candidate.len(),
        projected.text.len()
    );
    print_result("placeholder project", candidate.len(), project_elapsed);
    print_result("placeholder resolve", candidate.len(), resolve_elapsed);
    print_result("translated target rebind", candidate.len(), rebound_elapsed);
    print_result(
        "placeholder canonicalize",
        candidate.len(),
        canonical_elapsed,
    );
    print_result("placeholder reform", candidate.len(), reform_elapsed);
}

#[test]
#[ignore = "manual release-mode disk-spill throughput and resident-memory measurement"]
fn million_id_registry_spill_throughput() {
    let mut registry = BoundedIdRegistry::default();
    let started = Instant::now();
    for index in 0..REGISTRY_ID_COUNT {
        let id = format!("streamed-unit-{index}");
        assert!(registry.insert(&id).expect("unique ID should register"));
    }
    let insert_elapsed = started.elapsed();

    let lookup_started = Instant::now();
    for index in (0..REGISTRY_ID_COUNT).step_by(997) {
        let id = format!("streamed-unit-{index}");
        assert!(registry
            .contains(&id)
            .expect("registered ID should resolve"));
        assert!(!registry.insert(&id).expect("duplicate ID should resolve"));
    }
    let lookup_elapsed = lookup_started.elapsed();

    println!(
        "registry_ids={REGISTRY_ID_COUNT} insert={insert_elapsed:.3?} ({:.0} ids/s) sampled_duplicate_lookup={lookup_elapsed:.3?}",
        REGISTRY_ID_COUNT as f64 / insert_elapsed.as_secs_f64()
    );
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
