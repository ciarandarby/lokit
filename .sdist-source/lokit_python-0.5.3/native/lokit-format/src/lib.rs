#![forbid(unsafe_code)]

//! Parser, canonical formatter, typed model, and diagnostics for the Lokit
//! localization interchange format.

mod diagnostic;
#[doc(hidden)]
pub mod id_registry;
mod model;
mod parser;
pub mod placeholder;
mod validation;
mod writer;

pub use diagnostic::{
    Diagnostic, DiagnosticCode, DiagnosticSeverity, ErrorCode, ParseError, SourceMap,
    SourcePosition, SourceSpan, SpanKind, SpanRecord,
};
pub use model::{
    AdjacentContext, BaseStructure, CodePart, Comment, Data, Meta, Origin, Plural, PluralCategory,
    SegmentPart, Tags, TargetData, TargetTags, TextPart, TieData, TieType, TranslationStatus,
};
pub use parser::{
    parse_reader, parse_reader_with_options, parse_reader_with_spans,
    parse_reader_with_spans_and_options, parse_str, parse_str_with_options, parse_str_with_spans,
    parse_str_with_spans_and_options, ParseOptions, ParsedDocument, StreamingReader,
    MAX_LINE_BYTES,
};
pub use placeholder::{
    canonicalize_placeholders, detect_placeholders, literalize_data_placeholders,
    literalize_segment_placeholders, project_data_placeholders, project_placeholders,
    project_segment_placeholders, reform_placeholders, resolve_data_placeholders,
    resolve_segment_placeholders, CanonicalPlaceholderText, DetectionOptions, PlaceholderAnalysis,
    PlaceholderError, PlaceholderLimits, PlaceholderOccurrence, PlaceholderProjection,
    PlaceholderProjectionOptions, PlaceholderRole, PlaceholderSyntax, PlaceholderValueType,
    ReformedTarget, ResolvedPlaceholders,
};
pub use validation::{
    validate, validate_parsed, validate_parsed_with_limit, validate_unit, validate_unit_with_spans,
    validate_unit_with_spans_and_limit, StreamingValidator,
};
pub use writer::{
    format_source, format_source_preserving_comments, write_document, CanonicalWriter, WriteError,
};

/// The only schema version accepted and emitted by this crate.
pub const SCHEMA_VERSION: u32 = 1;

/// Required first line for a Lokit interchange document.
pub const MAGIC: &str = "@lokit 1";

/// Lokit integers are signed 64-bit values in every integer-bearing field.
pub const INTEGER_MIN: i64 = i64::MIN;

/// Lokit integers are signed 64-bit values in every integer-bearing field.
pub const INTEGER_MAX: i64 = i64::MAX;
