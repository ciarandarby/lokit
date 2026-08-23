use std::collections::HashSet;
use std::io::{self, BufRead, Cursor, Read};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use lokit_format::{
    BaseStructure, Data, Diagnostic as CoreDiagnostic,
    DiagnosticSeverity as CoreDiagnosticSeverity, ParseError, ParseOptions, StreamingReader,
    StreamingValidator, format_source_preserving_comments, parse_str_with_options,
};
use tower_lsp_server::ls_types::{
    CompletionItem, CompletionItemKind, CompletionList, CompletionTextEdit, Diagnostic,
    DiagnosticSeverity, DocumentSymbol, DocumentSymbolResponse, FoldingRange, Hover, HoverContents,
    Location, MarkupContent, MarkupKind, NumberOrString, Position, Range, SymbolInformation,
    SymbolKind, TextEdit, Uri,
};

use crate::document::{LineIndex, PositionEncoding};

pub(crate) const MAX_COMPLETIONS: usize = 128;
pub(crate) const MAX_DIAGNOSTICS: usize = 200;
pub(crate) const MAX_SYMBOLS: usize = 2_048;
pub(crate) const MAX_FOLDING_RANGES: usize = 4_096;
const MAX_LINE_BYTES: usize = 1024 * 1024;
const MAX_NESTING: usize = 16;

pub(crate) struct DocumentAnalysis {
    pub(crate) structure: Option<Arc<BaseStructure>>,
    pub(crate) diagnostics: Vec<Diagnostic>,
}

pub(crate) fn analyze_document(
    text: &str,
    line_index: &LineIndex,
    encoding: PositionEncoding,
    cancelled: &AtomicBool,
) -> Option<DocumentAnalysis> {
    let cursor = CancellableCursor::new(text.as_bytes(), cancelled);
    let mut reader = match StreamingReader::with_spans_and_options(cursor, parse_options()) {
        Ok(reader) => reader,
        Err(error) => return parse_failure(text, line_index, encoding, error, cancelled),
    };
    let mut document = reader.document_header().clone();
    let mut validator = StreamingValidator::default();
    let header_source_map = reader.take_source_map();
    let mut diagnostics = Vec::new();
    diagnostics.extend(
        validator
            .validate_document_header_with_spans_and_limit(
                &document,
                &header_source_map,
                MAX_DIAGNOSTICS,
            )
            .into_iter()
            .map(|diagnostic| lsp_diagnostic(text, line_index, encoding, diagnostic)),
    );
    let mut unit_index = 0_usize;
    loop {
        if cancelled.load(Ordering::Relaxed) {
            return None;
        }
        let unit = match reader.next_unit_allowing_duplicate_ids() {
            Ok(unit) => unit,
            Err(error) => return parse_failure(text, line_index, encoding, error, cancelled),
        };
        let Some((unit_id, data)) = unit else {
            return Some(DocumentAnalysis {
                structure: Some(Arc::new(document)),
                diagnostics,
            });
        };
        let source_map = reader.take_source_map();
        let remaining = MAX_DIAGNOSTICS.saturating_sub(diagnostics.len());
        diagnostics.extend(
            validator
                .validate_unit_with_spans_and_limit(
                    unit_index,
                    &unit_id,
                    &data,
                    &source_map,
                    remaining,
                )
                .into_iter()
                .map(|diagnostic| lsp_diagnostic(text, line_index, encoding, diagnostic)),
        );
        document.data.push((unit_id, data));
        unit_index = unit_index.saturating_add(1);
    }
}

fn lsp_diagnostic(
    text: &str,
    line_index: &LineIndex,
    encoding: PositionEncoding,
    diagnostic: CoreDiagnostic,
) -> Diagnostic {
    let range = diagnostic
        .span
        .as_ref()
        .and_then(|span| {
            line_index.range_for_bytes(text, span.bytes.start, span.bytes.end, encoding)
        })
        .unwrap_or_default();
    Diagnostic::new(
        range,
        Some(match diagnostic.severity {
            CoreDiagnosticSeverity::Error => DiagnosticSeverity::ERROR,
            CoreDiagnosticSeverity::Warning => DiagnosticSeverity::WARNING,
        }),
        Some(NumberOrString::String(diagnostic.code.as_str().to_owned())),
        Some("lokit".to_owned()),
        diagnostic.message,
        None,
        None,
    )
}

fn parse_failure(
    text: &str,
    line_index: &LineIndex,
    encoding: PositionEncoding,
    error: ParseError,
    cancelled: &AtomicBool,
) -> Option<DocumentAnalysis> {
    if cancelled.load(Ordering::Relaxed) {
        return None;
    }
    let range = line_index
        .range_for_bytes(text, error.span.bytes.start, error.span.bytes.end, encoding)
        .unwrap_or_default();
    Some(DocumentAnalysis {
        structure: None,
        diagnostics: vec![Diagnostic::new(
            range,
            Some(DiagnosticSeverity::ERROR),
            Some(NumberOrString::String(error.code.as_str().to_owned())),
            Some("lokit".to_owned()),
            error.message,
            None,
            None,
        )],
    })
}

struct CancellableCursor<'a> {
    inner: Cursor<&'a [u8]>,
    cancelled: &'a AtomicBool,
}

impl<'a> CancellableCursor<'a> {
    const fn new(bytes: &'a [u8], cancelled: &'a AtomicBool) -> Self {
        Self {
            inner: Cursor::new(bytes),
            cancelled,
        }
    }

    fn check_cancelled(&self) -> io::Result<()> {
        if self.cancelled.load(Ordering::Relaxed) {
            Err(io::Error::new(
                io::ErrorKind::Interrupted,
                "analysis cancelled",
            ))
        } else {
            Ok(())
        }
    }
}

impl Read for CancellableCursor<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.check_cancelled()?;
        self.inner.read(buffer)
    }
}

impl BufRead for CancellableCursor<'_> {
    fn fill_buf(&mut self) -> io::Result<&[u8]> {
        self.check_cancelled()?;
        self.inner.fill_buf()
    }

    fn consume(&mut self, amount: usize) {
        self.inner.consume(amount);
    }
}

const fn parse_options() -> ParseOptions {
    ParseOptions {
        max_line_bytes: MAX_LINE_BYTES,
        max_nesting: MAX_NESTING,
    }
}

pub(crate) fn canonical_text(source: &str, document: &BaseStructure) -> Result<String, String> {
    format_source_preserving_comments(source, document).map_err(|error| error.to_string())
}

pub(crate) fn parse_and_canonical_text(source: &str) -> Result<Option<String>, String> {
    let Ok(document) = parse_str_with_options(source, parse_options()) else {
        return Ok(None);
    };
    canonical_text(source, &document).map(Some)
}

const STATUSES: &[&str] = &[
    "new",
    "draft",
    "translated",
    "reviewed",
    "approved",
    "rejected",
    "unknown",
];
const PLURAL_CATEGORIES: &[&str] = &["generic", "zero", "one", "two", "few", "many", "other"];
const TIE_TYPES: &[&str] = &[
    "a.open",
    "a.close",
    "abbr.open",
    "abbr.close",
    "b.open",
    "b.close",
    "bdi.open",
    "bdi.close",
    "bdo.open",
    "bdo.close",
    "br.standalone",
    "cite.open",
    "cite.close",
    "code.open",
    "code.close",
    "data.open",
    "data.close",
    "dfn.open",
    "dfn.close",
    "em.open",
    "em.close",
    "i.open",
    "i.close",
    "img.standalone",
    "kbd.open",
    "kbd.close",
    "mark.open",
    "mark.close",
    "q.open",
    "q.close",
    "rp.open",
    "rp.close",
    "rt.open",
    "rt.close",
    "ruby.open",
    "ruby.close",
    "s.open",
    "s.close",
    "samp.open",
    "samp.close",
    "small.open",
    "small.close",
    "span.open",
    "span.close",
    "strong.open",
    "strong.close",
    "sub.open",
    "sub.close",
    "sup.open",
    "sup.close",
    "time.open",
    "time.close",
    "u.open",
    "u.close",
    "var.open",
    "var.close",
    "wbr.standalone",
    "custom.open",
    "custom.close",
    "custom.standalone",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BlockKind {
    Root,
    Document,
    Unit,
    Target,
    Plural,
    Meta,
    Comment,
    Origin,
    AdjacentContext,
    Tags,
    SourceParts,
    TargetParts,
    Parts,
    Tag,
    Other,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct BlockFrame {
    kind: BlockKind,
    name: String,
    label: Option<String>,
    start_line: u32,
    start_byte: usize,
    selection_start: usize,
    selection_end: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ScannedBlock {
    frame: BlockFrame,
    end_line: u32,
    end_byte: usize,
    children: Vec<Self>,
}

#[derive(Clone, Copy)]
struct Suggestion {
    label: &'static str,
    insert_text: &'static str,
    detail: &'static str,
    kind: CompletionItemKind,
}

impl Suggestion {
    const fn field(label: &'static str, insert_text: &'static str, detail: &'static str) -> Self {
        Self {
            label,
            insert_text,
            detail,
            kind: CompletionItemKind::FIELD,
        }
    }

    const fn block(label: &'static str, insert_text: &'static str, detail: &'static str) -> Self {
        Self {
            label,
            insert_text,
            detail,
            kind: CompletionItemKind::STRUCT,
        }
    }
}

const DOCUMENT_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field(
        "source_locale",
        "source_locale = \"\"",
        "Required source locale",
    ),
    Suggestion::field(
        "target_locale",
        "target_locale = \"\"",
        "Legacy target locale",
    ),
    Suggestion::field(
        "target_locales",
        "target_locales = []",
        "Ordered target locales",
    ),
    Suggestion::field(
        "format_version",
        "format_version = \"\"",
        "BaseStructure format version",
    ),
    Suggestion::field("export_origin", "export_origin = \"\"", "Export origin"),
    Suggestion::field(
        "export_timestamp",
        "export_timestamp = \"\"",
        "Export timestamp",
    ),
    Suggestion::field(
        "source_language",
        "source_language = \"\"",
        "Source language",
    ),
    Suggestion::field(
        "target_language",
        "target_language = \"\"",
        "Target language",
    ),
    Suggestion::field(
        "target_languages",
        "target_languages = []",
        "Ordered target languages",
    ),
    Suggestion::field(
        "extension",
        "extension \"\" = \"\"",
        "String extension entry",
    ),
];

const ROOT_SUGGESTIONS: &[Suggestion] = &[
    Suggestion {
        label: "@lokit 1",
        insert_text: "@lokit 1",
        detail: "Lokit interchange envelope magic",
        kind: CompletionItemKind::KEYWORD,
    },
    Suggestion::block(
        "document",
        "document {\n  source_locale = \"\"\n}",
        "Mandatory document metadata block",
    ),
    Suggestion::block(
        "unit",
        "unit \"\" {\n  source = \"\"\n}",
        "Translation unit",
    ),
];

const UNIT_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("source", "source = \"\"", "Required source text"),
    Suggestion::field("target", "target = \"\"", "Legacy target text"),
    Suggestion::block("target block", "target \"\" {\n}", "Locale-specific target"),
    Suggestion::field("status", "status = unknown", "Translation status"),
    Suggestion::block("plural", "plural {\n}", "Plural information"),
    Suggestion::block("tags", "tags {\n}", "Inline content"),
    Suggestion::block("meta", "meta {\n}", "Usage and lifecycle metadata"),
    Suggestion::block(
        "comment",
        "comment {\n  context = \"\"\n}",
        "Translator comment",
    ),
    Suggestion::block(
        "previous_context",
        "previous_context {\n}",
        "Previous unit context",
    ),
    Suggestion::block("next_context", "next_context {\n}", "Next unit context"),
    Suggestion::field(
        "extension",
        "extension \"\" = \"\"",
        "String extension entry",
    ),
];

const TARGET_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("text", "text = \"\"", "Target text"),
    Suggestion::field("status", "status = unknown", "Translation status"),
    Suggestion::block("plural", "plural {\n}", "Plural information"),
    Suggestion::block("tags", "tags {\n}", "Target inline content"),
    Suggestion::block("meta", "meta {\n}", "Usage and lifecycle metadata"),
    Suggestion::block(
        "comment",
        "comment {\n  context = \"\"\n}",
        "Translator comment",
    ),
    Suggestion::field(
        "extension",
        "extension \"\" = \"\"",
        "String extension entry",
    ),
];

const PLURAL_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("variant", "variant = \"\"", "Required plural variant"),
    Suggestion::field("count", "count = 0", "Exact plural count"),
    Suggestion::field("category", "category = other", "CLDR plural category"),
    Suggestion::field(
        "extension",
        "extension \"\" = \"\"",
        "String extension entry",
    ),
];

const META_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("usage_count", "usage_count = 0", "Usage count"),
    Suggestion::field("last_used", "last_used = \"\"", "Last-used timestamp"),
    Suggestion::field("first_used", "first_used = \"\"", "First-used timestamp"),
    Suggestion::field("created", "created = \"\"", "Creation timestamp"),
    Suggestion::field("updated", "updated = \"\"", "Update timestamp"),
    Suggestion::field("max_length", "max_length = 0", "Maximum translation length"),
    Suggestion::field("min_length", "min_length = 0", "Minimum translation length"),
    Suggestion::field(
        "extension",
        "extension \"\" = \"\"",
        "String extension entry",
    ),
];

const COMMENT_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("context", "context = \"\"", "Required comment text/context"),
    Suggestion::field("timestamp", "timestamp = \"\"", "Comment timestamp"),
    Suggestion::field("context_key", "context_key = \"\"", "Comment context key"),
    Suggestion::block("origin", "origin {\n}", "Comment provenance"),
    Suggestion::field(
        "extension",
        "extension \"\" = \"\"",
        "String extension entry",
    ),
];

const ORIGIN_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("system", "system = \"\"", "Origin system"),
    Suggestion::field("project", "project = \"\"", "Origin project"),
    Suggestion::field(
        "creator_id",
        "creator_id = \"\"",
        "Origin creator identifier",
    ),
    Suggestion::field(
        "extension",
        "extension \"\" = \"\"",
        "String extension entry",
    ),
];

const CONTEXT_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("unit_id", "unit_id = \"\"", "Adjacent unit identifier"),
    Suggestion::field("source", "source = \"\"", "Adjacent source text"),
    Suggestion::field("target", "target = \"\"", "Adjacent target text"),
    Suggestion::field(
        "extension",
        "extension \"\" = \"\"",
        "String extension entry",
    ),
];

const UNIT_TAGS_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::block(
        "source_tag",
        "source_tag \"\" {\n  id = \"\"\n  type = custom.standalone\n}",
        "Source tag definition",
    ),
    Suggestion::block(
        "target_tag",
        "target_tag \"\" {\n  id = \"\"\n  type = custom.standalone\n}",
        "Legacy target tag definition",
    ),
    Suggestion::block("source_parts", "source_parts {\n}", "Ordered source parts"),
    Suggestion::block(
        "target_parts",
        "target_parts {\n}",
        "Ordered legacy target parts",
    ),
];

const TARGET_TAGS_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::block(
        "tag",
        "tag \"\" {\n  id = \"\"\n  type = custom.standalone\n}",
        "Target tag definition",
    ),
    Suggestion::block("parts", "parts {\n}", "Ordered target parts"),
];

const TAG_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("id", "id = \"\"", "TieData identifier"),
    Suggestion::field("type", "type = custom.standalone", "Inline tag type"),
    Suggestion::field("attribute", "attribute \"\" = \"\"", "Inline tag attribute"),
    Suggestion::field(
        "attribute_data",
        "attribute_data = \"\"",
        "Original attribute text",
    ),
    Suggestion::field("position", "position = 0", "Original position"),
    Suggestion::field("order", "order = 0", "Original order"),
    Suggestion::field("pair_id", "pair_id = \"\"", "Open/close pair identifier"),
    Suggestion::field("original_name", "original_name = \"\"", "Original tag name"),
    Suggestion::field("original_text", "original_text = \"\"", "Original tag text"),
];

const PARTS_SUGGESTIONS: &[Suggestion] = &[
    Suggestion::field("text", "text = \"\"", "Literal segment part"),
    Suggestion::field("code", "code = \"\"", "Reference to a tag-map key"),
];

#[allow(clippy::too_many_lines)]
pub(crate) fn completions(
    text: &str,
    line_index: &LineIndex,
    position: Position,
    encoding: PositionEncoding,
    structure: Option<&BaseStructure>,
    supported_kinds: Option<&[CompletionItemKind]>,
) -> CompletionList {
    let Some(offset) = line_index.byte_offset(text, position, encoding) else {
        return CompletionList::default();
    };
    let frames = context_at(text, line_index, offset);
    let line_number = usize::try_from(position.line).unwrap_or(usize::MAX);
    let Some((line_start, line_end)) = line_index.line_bounds(text, line_number) else {
        return CompletionList::default();
    };
    let line_prefix = text.get(line_start..offset).unwrap_or_default();
    let prefix = line_prefix.trim_start();
    let indentation = line_prefix
        .get(..line_prefix.len().saturating_sub(prefix.len()))
        .unwrap_or_default();

    if let Some(value_context) = target_value_context(line_prefix, line_start) {
        let candidates = locale_candidates(text, line_index, structure);
        return value_completions(
            candidates,
            value_context.typed_prefix,
            "Document target locale",
            CompletionItemKind::VALUE,
            completion_edit_range(
                line_index,
                text,
                value_context.replace_start,
                value_replacement_end(text, value_context.replace_start, offset, line_end),
                encoding,
            ),
            true,
            supported_kinds,
        );
    }

    if let Some((field, value_context)) = assignment_value_context(line_prefix, line_start) {
        let values = match field {
            "status" => Some(STATUSES),
            "category" => Some(PLURAL_CATEGORIES),
            "type" => Some(TIE_TYPES),
            _ => None,
        };
        if let Some(values) = values {
            return value_completions(
                values.iter().map(|value| (*value).to_owned()).collect(),
                value_context.typed_prefix,
                "Lokit enum value",
                CompletionItemKind::ENUM_MEMBER,
                completion_edit_range(
                    line_index,
                    text,
                    value_context.replace_start,
                    value_replacement_end(text, value_context.replace_start, offset, line_end),
                    encoding,
                ),
                false,
                supported_kinds,
            );
        }
        if field == "code" {
            let candidates = code_reference_candidates(text, line_index, structure, &frames);
            return value_completions(
                candidates,
                value_context.typed_prefix,
                "Tag-map key",
                CompletionItemKind::REFERENCE,
                completion_edit_range(
                    line_index,
                    text,
                    value_context.replace_start,
                    value_replacement_end(text, value_context.replace_start, offset, line_end),
                    encoding,
                ),
                true,
                supported_kinds,
            );
        }
    }

    if prefix.starts_with('#') || inside_json_string(line_prefix) {
        return CompletionList::default();
    }

    let current = frames.last().map_or(BlockKind::Root, |frame| frame.kind);
    let suggestions = match current {
        BlockKind::Root => ROOT_SUGGESTIONS,
        BlockKind::Document => DOCUMENT_SUGGESTIONS,
        BlockKind::Unit => UNIT_SUGGESTIONS,
        BlockKind::Target => TARGET_SUGGESTIONS,
        BlockKind::Plural => PLURAL_SUGGESTIONS,
        BlockKind::Meta => META_SUGGESTIONS,
        BlockKind::Comment => COMMENT_SUGGESTIONS,
        BlockKind::Origin => ORIGIN_SUGGESTIONS,
        BlockKind::AdjacentContext => CONTEXT_SUGGESTIONS,
        BlockKind::Tags if inside_target(&frames) => TARGET_TAGS_SUGGESTIONS,
        BlockKind::Tags => UNIT_TAGS_SUGGESTIONS,
        BlockKind::SourceParts | BlockKind::TargetParts | BlockKind::Parts => PARTS_SUGGESTIONS,
        BlockKind::Tag => TAG_SUGGESTIONS,
        BlockKind::Other => &[][..],
    };
    let items = suggestions
        .iter()
        .take(MAX_COMPLETIONS)
        .map(|suggestion| completion_from_suggestion(suggestion, indentation, supported_kinds))
        .collect();
    CompletionList {
        is_incomplete: suggestions.len() > MAX_COMPLETIONS,
        items,
    }
}

fn completion_from_suggestion(
    suggestion: &Suggestion,
    indentation: &str,
    supported_kinds: Option<&[CompletionItemKind]>,
) -> CompletionItem {
    let insert_text = if suggestion.insert_text.contains('\n') {
        suggestion
            .insert_text
            .replace('\n', &format!("\n{indentation}"))
    } else {
        suggestion.insert_text.to_owned()
    };
    CompletionItem {
        label: suggestion.label.to_owned(),
        kind: completion_kind(suggestion.kind, supported_kinds),
        detail: Some(suggestion.detail.to_owned()),
        insert_text: Some(insert_text),
        ..CompletionItem::default()
    }
}

fn completion_kind(
    desired: CompletionItemKind,
    supported: Option<&[CompletionItemKind]>,
) -> Option<CompletionItemKind> {
    let baseline = if desired == CompletionItemKind::STRUCT {
        CompletionItemKind::CLASS
    } else if desired == CompletionItemKind::ENUM_MEMBER {
        CompletionItemKind::ENUM
    } else if matches!(
        desired,
        CompletionItemKind::FOLDER
            | CompletionItemKind::CONSTANT
            | CompletionItemKind::EVENT
            | CompletionItemKind::OPERATOR
            | CompletionItemKind::TYPE_PARAMETER
    ) {
        CompletionItemKind::TEXT
    } else {
        desired
    };
    let Some(supported) = supported else {
        return Some(baseline);
    };
    if supported.contains(&desired) {
        Some(desired)
    } else if supported.contains(&baseline) {
        Some(baseline)
    } else if supported.contains(&CompletionItemKind::TEXT) {
        Some(CompletionItemKind::TEXT)
    } else {
        supported.first().copied()
    }
}

fn locale_candidates(
    text: &str,
    line_index: &LineIndex,
    structure: Option<&BaseStructure>,
) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut values = Vec::new();
    for line in source_lines(text, line_index) {
        let trimmed = line.trim();
        if let Some(value) = string_assignment(trimmed, "target_locale") {
            values.push(value);
        }
        if let Some(items) = string_list_assignment(trimmed, "target_locales") {
            values.extend(items);
        }
        if let Some(items) = string_list_assignment(trimmed, "target_languages") {
            values.extend(items);
        }
        if let Some(frame) = opening_frame(trimmed, 0, 0) {
            if frame.kind == BlockKind::Target {
                if let Some(locale) = frame.label {
                    values.push(locale);
                }
            }
        }
    }
    if let Some(structure) = structure {
        values.extend(structure.target_locale.iter().cloned());
        values.extend(structure.target_locales.iter().cloned());
        values.extend(structure.target_languages.iter().cloned());
        for (_, unit) in &structure.data {
            values.extend(unit.targets.iter().map(|(locale, _)| locale.clone()));
        }
    }
    values.retain(|value| seen.insert(value.clone()));
    values
}

fn code_reference_candidates(
    text: &str,
    line_index: &LineIndex,
    structure: Option<&BaseStructure>,
    frames: &[BlockFrame],
) -> Vec<String> {
    let mut values = lexical_tag_keys(text, line_index, frames);
    let Some(structure) = structure else {
        return values;
    };
    let unit_id = frames
        .iter()
        .rev()
        .find(|frame| frame.kind == BlockKind::Unit)
        .and_then(|frame| frame.label.as_deref());
    let Some((_, unit)) =
        unit_id.and_then(|id| structure.data.iter().find(|(candidate, _)| candidate == id))
    else {
        return values;
    };

    let mut seen = HashSet::new();
    values.extend(relevant_tag_keys(unit, frames).into_iter().cloned());
    values.retain(|value| seen.insert(value.clone()));
    values
}

#[derive(Clone, Copy)]
struct ValueContext<'a> {
    typed_prefix: &'a str,
    replace_start: usize,
}

fn target_value_context(line_prefix: &str, line_start: usize) -> Option<ValueContext<'_>> {
    let trimmed = line_prefix.trim_start();
    let indentation = line_prefix.len().checked_sub(trimmed.len())?;
    let tail = trimmed.strip_prefix("target")?;
    if tail.is_empty() || !tail.as_bytes().first()?.is_ascii_whitespace() || tail.contains('=') {
        return None;
    }
    let value = tail.trim_start();
    if !value_context_is_active(value) {
        return None;
    }
    let value_offset = trimmed
        .len()
        .checked_sub(value.len())?
        .checked_add(indentation)?;
    Some(ValueContext {
        typed_prefix: typed_value_prefix(value),
        replace_start: line_start.checked_add(value_offset)?,
    })
}

fn assignment_value_context(
    line_prefix: &str,
    line_start: usize,
) -> Option<(&str, ValueContext<'_>)> {
    let trimmed = line_prefix.trim_start();
    let indentation = line_prefix.len().checked_sub(trimmed.len())?;
    let (left, right) = trimmed.split_once('=')?;
    let field = left.split_whitespace().next()?;
    let value = right.trim_start();
    if !value_context_is_active(value) {
        return None;
    }
    let value_offset = trimmed
        .len()
        .checked_sub(right.len())?
        .checked_add(right.len().checked_sub(value.len())?)?
        .checked_add(indentation)?;
    Some((
        field,
        ValueContext {
            typed_prefix: typed_value_prefix(value),
            replace_start: line_start.checked_add(value_offset)?,
        },
    ))
}

fn typed_value_prefix(value: &str) -> &str {
    value
        .strip_prefix('"')
        .unwrap_or(value)
        .split('"')
        .next()
        .unwrap_or_default()
}

fn value_context_is_active(value: &str) -> bool {
    if value.starts_with('"') {
        let mut escaped = false;
        for (index, byte) in value.bytes().enumerate().skip(1) {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                return index.saturating_add(1) == value.len();
            }
        }
        true
    } else {
        !value
            .bytes()
            .any(|byte| byte.is_ascii_whitespace() || matches!(byte, b'{' | b'}'))
    }
}

fn completion_edit_range(
    line_index: &LineIndex,
    text: &str,
    start: usize,
    end: usize,
    encoding: PositionEncoding,
) -> Option<Range> {
    line_index.range_for_bytes(text, start, end, encoding)
}

fn value_replacement_end(text: &str, start: usize, cursor: usize, line_end: usize) -> usize {
    let bytes = text.as_bytes();
    if bytes.get(start) == Some(&b'"') {
        let mut escaped = false;
        let mut index = start.saturating_add(1);
        while index < line_end {
            let Some(byte) = bytes.get(index).copied() else {
                break;
            };
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                return index.saturating_add(1);
            }
            index += 1;
        }
        return cursor;
    }

    let mut end = start;
    while end < line_end {
        let Some(byte) = bytes.get(end).copied() else {
            break;
        };
        if byte.is_ascii_whitespace() || matches!(byte, b'{' | b'}') {
            break;
        }
        end += 1;
    }
    end.max(cursor)
}

#[allow(clippy::too_many_arguments)]
fn value_completions(
    candidates: Vec<String>,
    typed_prefix: &str,
    detail: &str,
    desired_kind: CompletionItemKind,
    edit_range: Option<Range>,
    quote: bool,
    supported_kinds: Option<&[CompletionItemKind]>,
) -> CompletionList {
    let mut seen = HashSet::new();
    let mut matching = 0_usize;
    let mut items = Vec::new();
    for value in candidates {
        if !value.starts_with(typed_prefix) || !seen.insert(value.clone()) {
            continue;
        }
        matching += 1;
        if items.len() >= MAX_COMPLETIONS {
            continue;
        }
        let new_text = if quote {
            serde_json::to_string(&value).unwrap_or_else(|_| format!("\"{value}\""))
        } else {
            value.clone()
        };
        items.push(CompletionItem {
            label: value,
            kind: completion_kind(desired_kind, supported_kinds),
            detail: Some(detail.to_owned()),
            text_edit: edit_range
                .map(|range| CompletionTextEdit::Edit(TextEdit::new(range, new_text))),
            ..CompletionItem::default()
        });
    }
    CompletionList {
        is_incomplete: matching > MAX_COMPLETIONS,
        items,
    }
}

fn source_lines<'a>(text: &'a str, line_index: &'a LineIndex) -> impl Iterator<Item = &'a str> {
    (0..line_index.line_count()).filter_map(|line| {
        let (start, end) = line_index.line_bounds(text, line)?;
        text.get(start..end)
    })
}

fn string_assignment(line: &str, expected_field: &str) -> Option<String> {
    let (field, value) = line.split_once('=')?;
    (field.trim() == expected_field)
        .then(|| serde_json::from_str::<String>(value.trim()).ok())
        .flatten()
}

fn string_list_assignment(line: &str, expected_field: &str) -> Option<Vec<String>> {
    let (field, value) = line.split_once('=')?;
    (field.trim() == expected_field)
        .then(|| serde_json::from_str::<Vec<String>>(value.trim()).ok())
        .flatten()
}

fn lexical_tag_keys(text: &str, line_index: &LineIndex, frames: &[BlockFrame]) -> Vec<String> {
    let current_unit = frames
        .iter()
        .rev()
        .find(|frame| frame.kind == BlockKind::Unit)
        .map(|frame| frame.start_byte);
    let Some(current_unit) = current_unit else {
        return Vec::new();
    };
    let current_target = frames
        .iter()
        .rev()
        .find(|frame| frame.kind == BlockKind::Target)
        .map(|frame| frame.start_byte);
    let in_source_parts = frames
        .iter()
        .any(|frame| frame.kind == BlockKind::SourceParts);
    let in_target_parts = frames
        .iter()
        .any(|frame| frame.kind == BlockKind::TargetParts);
    let in_target_parts_block =
        frames.iter().any(|frame| frame.kind == BlockKind::Parts) && current_target.is_some();

    let mut values = Vec::new();
    let mut stack: Vec<BlockFrame> = Vec::new();
    let mut suppressed_depth = 0_usize;
    for line_number in 0..line_index.line_count() {
        let Some((start, end)) = line_index.line_bounds(text, line_number) else {
            continue;
        };
        let Some(line) = text.get(start..end) else {
            continue;
        };
        let leading_trimmed = line.trim_start();
        let indentation = line.len().saturating_sub(leading_trimmed.len());
        let trimmed = leading_trimmed.trim_end();
        if trimmed == "}" {
            if suppressed_depth == 0 {
                stack.pop();
            } else {
                suppressed_depth -= 1;
            }
            continue;
        }
        let Some(frame) = opening_frame(
            trimmed,
            u32::try_from(line_number).unwrap_or(u32::MAX),
            start.saturating_add(indentation),
        ) else {
            continue;
        };
        if frame.kind == BlockKind::Tag {
            if let Some(key) = frame.label.clone() {
                let unit = stack
                    .iter()
                    .rev()
                    .find(|candidate| candidate.kind == BlockKind::Unit)
                    .map(|candidate| candidate.start_byte);
                let target = stack
                    .iter()
                    .rev()
                    .find(|candidate| candidate.kind == BlockKind::Target)
                    .map(|candidate| candidate.start_byte);
                let matches_scope = unit == Some(current_unit)
                    && ((in_source_parts && frame.name == "source_tag" && target.is_none())
                        || (in_target_parts && frame.name == "target_tag" && target.is_none())
                        || (in_target_parts_block
                            && frame.name == "tag"
                            && target == current_target));
                if matches_scope {
                    values.push(key);
                }
            }
        }
        if stack.len() < 32 {
            stack.push(frame);
        } else {
            suppressed_depth += 1;
        }
    }
    values
}

fn relevant_tag_keys<'a>(unit: &'a Data, frames: &[BlockFrame]) -> Vec<&'a String> {
    if frames
        .iter()
        .any(|frame| frame.kind == BlockKind::SourceParts)
    {
        return unit.tags.as_ref().map_or_else(Vec::new, |tags| {
            tags.source_tag_map.iter().map(|(key, _)| key).collect()
        });
    }
    if frames
        .iter()
        .any(|frame| frame.kind == BlockKind::TargetParts)
    {
        return unit.tags.as_ref().map_or_else(Vec::new, |tags| {
            tags.target_tag_map.iter().map(|(key, _)| key).collect()
        });
    }
    if let Some(locale) = frames
        .iter()
        .rev()
        .find(|frame| frame.kind == BlockKind::Target)
        .and_then(|frame| frame.label.as_deref())
    {
        return unit
            .targets
            .iter()
            .find(|(candidate, _)| candidate == locale)
            .and_then(|(_, target)| target.tags.as_ref())
            .map_or_else(Vec::new, |tags| {
                tags.tag_map.iter().map(|(key, _)| key).collect()
            });
    }

    let mut keys = Vec::new();
    if let Some(tags) = unit.tags.as_ref() {
        keys.extend(tags.source_tag_map.iter().map(|(key, _)| key));
        keys.extend(tags.target_tag_map.iter().map(|(key, _)| key));
    }
    for (_, target) in &unit.targets {
        if let Some(tags) = target.tags.as_ref() {
            keys.extend(tags.tag_map.iter().map(|(key, _)| key));
        }
    }
    keys
}

fn inside_target(frames: &[BlockFrame]) -> bool {
    frames
        .iter()
        .rev()
        .skip(1)
        .take_while(|frame| frame.kind != BlockKind::Unit)
        .any(|frame| frame.kind == BlockKind::Target)
}

pub(crate) fn hover(
    text: &str,
    line_index: &LineIndex,
    position: Position,
    encoding: PositionEncoding,
    markup_kind: MarkupKind,
) -> Option<Hover> {
    let offset = line_index.byte_offset(text, position, encoding)?;
    let (start, end, token) = token_at(text, offset)?;
    let line_number = usize::try_from(position.line).ok()?;
    let (line_start, _) = line_index.line_bounds(text, line_number)?;
    let line_prefix = text.get(line_start..start)?;
    if line_prefix.trim_start().starts_with('#') || inside_json_string(line_prefix) {
        return None;
    }
    let description = schema_help(token)?;
    let value = if markup_kind == MarkupKind::Markdown {
        format!("`{token}`\n\n{description}")
    } else {
        format!("{token}\n\n{description}")
    };
    Some(Hover {
        contents: HoverContents::Markup(MarkupContent {
            kind: markup_kind,
            value,
        }),
        range: line_index.range_for_bytes(text, start, end, encoding),
    })
}

fn inside_json_string(prefix: &str) -> bool {
    let mut quoted = false;
    let mut escaped = false;
    for byte in prefix.bytes() {
        if escaped {
            escaped = false;
        } else if byte == b'\\' && quoted {
            escaped = true;
        } else if byte == b'"' {
            quoted = !quoted;
        }
    }
    quoted
}

fn schema_help(token: &str) -> Option<&'static str> {
    match token {
        "@lokit" => Some("Lokit interchange envelope magic. Version 1 is currently supported."),
        "document" => {
            Some("Mandatory document metadata block; it must precede every translation unit.")
        }
        "unit" => Some("A translation unit keyed by its quoted identifier."),
        "source_locale" => {
            Some("Required source locale. The interchange layer preserves the exact string.")
        }
        "target_locale" => Some("Optional legacy single target locale."),
        "target_locales" => Some("Ordered list of target locale strings."),
        "format_version" => Some(
            "The BaseStructure format-version value. It is independent of the `@lokit 1` schema envelope.",
        ),
        "export_origin" => Some("The system or tool that produced the exported structure."),
        "export_timestamp" => Some("The structure's export timestamp, preserved as text."),
        "source_language" => Some("Optional source-language value preserved exactly as text."),
        "target_language" => Some("Optional legacy single target-language value."),
        "target_languages" => Some("Ordered list of target-language values."),
        "source" => Some(
            "Required source text in a unit, or adjacent source context inside a context block.",
        ),
        "target" => {
            Some("Legacy target text, adjacent target context, or a locale-keyed TargetData block.")
        }
        "text" => {
            Some("Target text. Omission means `None`; `\"\"` is an explicitly empty translation.")
        }
        "status" => Some("Translation workflow status. `unknown` is the sparse default."),
        "plural" => Some("Plural information. The nested `variant` field is required."),
        "variant" => Some("Required plural text variant."),
        "count" => Some("Optional exact plural count. Zero is distinct from omission."),
        "category" => Some("Plural category: generic, zero, one, two, few, many, or other."),
        "meta" => Some(
            "Usage, timestamp, and length metadata. An omitted block reconstructs default Meta.",
        ),
        "usage_count" => Some("Optional usage count. Zero is distinct from omission."),
        "last_used" => Some("Optional last-used timestamp preserved as text."),
        "first_used" => Some("Optional first-used timestamp preserved as text."),
        "created" => Some("Optional creation timestamp preserved as text."),
        "updated" => Some("Optional update timestamp preserved as text."),
        "max_length" => Some("Optional maximum target length. Zero is distinct from omission."),
        "min_length" => Some("Optional minimum target length. Zero is distinct from omission."),
        "comment" => Some("Ordered translator/developer comment block. `context` is required."),
        "context" => Some("Required comment context text."),
        "timestamp" => Some("Optional comment timestamp preserved as text."),
        "context_key" => Some("Optional key identifying the comment's context."),
        "origin" => {
            Some("Optional comment provenance. An explicit empty block preserves object presence.")
        }
        "system" => Some("Optional originating system name."),
        "project" => Some("Optional originating project name."),
        "creator_id" => Some("Optional identifier of the comment creator."),
        "previous_context" | "next_context" => {
            Some("Optional adjacent-unit context; an empty block preserves presence.")
        }
        "unit_id" => Some("Optional identifier of an adjacent translation unit."),
        "tags" => {
            Some("Inline tag definitions and the ordered text/code parts that reference them.")
        }
        "source_tag" | "target_tag" | "tag" => {
            Some("A map entry containing a TieData inline-code definition.")
        }
        "source_parts" | "target_parts" | "parts" => {
            Some("Ordered `text` and `code` segment parts.")
        }
        "code" => Some(
            "Reference to the quoted map key of a tag definition in the corresponding tags block.",
        ),
        "id" => Some("Required TieData identifier, independent of the tag-map key."),
        "type" => Some(
            "Inline tag classification such as `strong.open`, `strong.close`, or `br.standalone`.",
        ),
        "attribute_data" => Some("Original attribute source text for an inline tag."),
        "position" => Some("Original inline-tag position; zero is the sparse default."),
        "order" => Some("Original inline-tag order; zero is the sparse default."),
        "pair_id" => Some("Optional identifier joining an opening tag with its closing tag."),
        "original_name" => Some("Optional original inline-tag name."),
        "original_text" => Some("Optional original inline-tag source text."),
        "extension" => Some(
            "An ordered string-to-string extension entry. Duplicate keys in one scope are invalid.",
        ),
        "attribute" => Some("An ordered string-to-string inline tag attribute entry."),
        "new" | "draft" | "translated" | "reviewed" | "approved" | "rejected" | "unknown" => {
            Some("A Lokit translation status value.")
        }
        "generic" | "zero" | "one" | "two" | "few" | "many" | "other" => {
            Some("A Lokit plural category value.")
        }
        _ if TIE_TYPES.contains(&token) => Some("A Lokit inline tag type value."),
        _ => None,
    }
}

fn token_at(text: &str, offset: usize) -> Option<(usize, usize, &str)> {
    let bytes = text.as_bytes();
    let mut cursor = offset.min(bytes.len());
    if cursor > 0 && (cursor == bytes.len() || !is_token_byte(*bytes.get(cursor)?)) {
        cursor -= 1;
    }
    if !is_token_byte(*bytes.get(cursor)?) {
        return None;
    }
    let mut start = cursor;
    while start > 0 && is_token_byte(bytes[start - 1]) {
        start -= 1;
    }
    let mut end = cursor + 1;
    while end < bytes.len() && is_token_byte(bytes[end]) {
        end += 1;
    }
    Some((start, end, text.get(start..end)?))
}

const fn is_token_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'@')
}

pub(crate) fn document_symbols(
    text: &str,
    line_index: &LineIndex,
    encoding: PositionEncoding,
    uri: &Uri,
    hierarchical: bool,
    supported_kinds: Option<&[SymbolKind]>,
    cancelled: &AtomicBool,
) -> Option<DocumentSymbolResponse> {
    let mut budget = MAX_SYMBOLS;
    let blocks = scan_blocks(text, line_index, MAX_SYMBOLS, cancelled)?;
    if cancelled.load(Ordering::Relaxed) {
        return None;
    }
    Some(if hierarchical {
        DocumentSymbolResponse::Nested(
            blocks
                .iter()
                .filter_map(|block| {
                    nested_symbol_from_block(
                        block,
                        text,
                        line_index,
                        encoding,
                        supported_kinds,
                        &mut budget,
                    )
                })
                .collect(),
        )
    } else {
        let mut symbols = Vec::new();
        for block in &blocks {
            collect_flat_symbols(
                block,
                text,
                line_index,
                encoding,
                uri,
                supported_kinds,
                None,
                &mut budget,
                &mut symbols,
            );
        }
        DocumentSymbolResponse::Flat(symbols)
    })
}

fn nested_symbol_from_block(
    block: &ScannedBlock,
    text: &str,
    line_index: &LineIndex,
    encoding: PositionEncoding,
    supported_kinds: Option<&[SymbolKind]>,
    budget: &mut usize,
) -> Option<DocumentSymbol> {
    if *budget == 0 {
        return None;
    }
    *budget -= 1;
    let range =
        line_index.range_for_bytes(text, block.frame.start_byte, block.end_byte, encoding)?;
    let selection_range = line_index.range_for_bytes(
        text,
        block.frame.selection_start,
        block.frame.selection_end,
        encoding,
    )?;
    let children: Vec<_> = block
        .children
        .iter()
        .filter_map(|child| {
            nested_symbol_from_block(child, text, line_index, encoding, supported_kinds, budget)
        })
        .collect();
    let name = symbol_name(&block.frame);
    let kind = supported_symbol_kind(desired_symbol_kind(block.frame.kind), supported_kinds);
    #[allow(deprecated)]
    Some(DocumentSymbol {
        name,
        detail: None,
        kind,
        tags: None,
        deprecated: None,
        range,
        selection_range,
        children: (!children.is_empty()).then_some(children),
    })
}

#[allow(clippy::too_many_arguments)]
fn collect_flat_symbols(
    block: &ScannedBlock,
    text: &str,
    line_index: &LineIndex,
    encoding: PositionEncoding,
    uri: &Uri,
    supported_kinds: Option<&[SymbolKind]>,
    container_name: Option<&str>,
    budget: &mut usize,
    symbols: &mut Vec<SymbolInformation>,
) {
    if *budget == 0 {
        return;
    }
    let Some(selection_range) = line_index.range_for_bytes(
        text,
        block.frame.selection_start,
        block.frame.selection_end,
        encoding,
    ) else {
        return;
    };
    *budget -= 1;
    let name = symbol_name(&block.frame);
    #[allow(deprecated)]
    symbols.push(SymbolInformation {
        name: name.clone(),
        kind: supported_symbol_kind(desired_symbol_kind(block.frame.kind), supported_kinds),
        tags: None,
        deprecated: None,
        location: Location::new(uri.clone(), selection_range),
        container_name: container_name.map(str::to_owned),
    });
    for child in &block.children {
        collect_flat_symbols(
            child,
            text,
            line_index,
            encoding,
            uri,
            supported_kinds,
            Some(&name),
            budget,
            symbols,
        );
    }
}

fn symbol_name(frame: &BlockFrame) -> String {
    frame.label.as_ref().map_or_else(
        || frame.name.clone(),
        |label| format!("{} {label}", frame.name),
    )
}

const fn desired_symbol_kind(kind: BlockKind) -> SymbolKind {
    match kind {
        BlockKind::Document => SymbolKind::FILE,
        BlockKind::Unit | BlockKind::Target | BlockKind::Tag => SymbolKind::OBJECT,
        _ => SymbolKind::STRUCT,
    }
}

fn supported_symbol_kind(desired: SymbolKind, supported: Option<&[SymbolKind]>) -> SymbolKind {
    let baseline = if desired == SymbolKind::OBJECT || desired == SymbolKind::STRUCT {
        SymbolKind::CLASS
    } else {
        desired
    };
    let Some(supported) = supported else {
        return baseline;
    };
    if supported.contains(&desired) {
        desired
    } else if supported.contains(&baseline) {
        baseline
    } else if supported.contains(&SymbolKind::FILE) {
        SymbolKind::FILE
    } else {
        supported.first().copied().unwrap_or(SymbolKind::FILE)
    }
}

pub(crate) fn folding_ranges(
    text: &str,
    line_index: &LineIndex,
    maximum_ranges: usize,
    collapsed_text: bool,
    cancelled: &AtomicBool,
) -> Option<Vec<FoldingRange>> {
    let maximum_ranges = maximum_ranges.min(MAX_FOLDING_RANGES);
    if maximum_ranges == 0 {
        return Some(Vec::new());
    }
    let mut ranges = Vec::new();
    collect_folding_ranges(
        &scan_blocks(text, line_index, maximum_ranges, cancelled)?,
        &mut ranges,
        maximum_ranges,
        collapsed_text,
    );
    if cancelled.load(Ordering::Relaxed) {
        return None;
    }
    ranges.truncate(maximum_ranges);
    Some(ranges)
}

fn collect_folding_ranges(
    blocks: &[ScannedBlock],
    ranges: &mut Vec<FoldingRange>,
    maximum_ranges: usize,
    collapsed_text: bool,
) {
    for block in blocks {
        if ranges.len() >= maximum_ranges {
            return;
        }
        if block.end_line > block.frame.start_line {
            ranges.push(FoldingRange {
                start_line: block.frame.start_line,
                start_character: None,
                end_line: block.end_line,
                end_character: None,
                kind: None,
                collapsed_text: collapsed_text.then(|| symbol_name(&block.frame)),
            });
        }
        collect_folding_ranges(&block.children, ranges, maximum_ranges, collapsed_text);
    }
}

fn scan_blocks(
    text: &str,
    line_index: &LineIndex,
    maximum_blocks: usize,
    cancelled: &AtomicBool,
) -> Option<Vec<ScannedBlock>> {
    let mut roots = Vec::new();
    let mut stack: Vec<(BlockFrame, Vec<ScannedBlock>)> = Vec::new();
    let mut suppressed_depth = 0_usize;
    let mut block_count = 0_usize;
    for line_index_number in 0..line_index.line_count() {
        if line_index_number.trailing_zeros() >= 8 && cancelled.load(Ordering::Relaxed) {
            return None;
        }
        let Some((line_start, line_end)) = line_index.line_bounds(text, line_index_number) else {
            continue;
        };
        let Some(line) = text.get(line_start..line_end) else {
            continue;
        };
        let leading_trimmed = line.trim_start();
        let indentation = line.len().saturating_sub(leading_trimmed.len());
        let trimmed = leading_trimmed.trim_end();
        let line_number = u32::try_from(line_index_number).unwrap_or(u32::MAX);
        if trimmed == "}" {
            if suppressed_depth > 0 {
                suppressed_depth -= 1;
            } else if let Some((frame, children)) = stack.pop() {
                let block = ScannedBlock {
                    frame,
                    end_line: line_number,
                    end_byte: line_end,
                    children,
                };
                if let Some((_, parent_children)) = stack.last_mut() {
                    parent_children.push(block);
                } else {
                    roots.push(block);
                }
            }
        } else if let Some(frame) =
            opening_frame(trimmed, line_number, line_start.saturating_add(indentation))
        {
            if block_count >= maximum_blocks {
                suppressed_depth += 1;
                continue;
            }
            block_count += 1;
            stack.push((frame, Vec::new()));
        }
    }
    Some(roots)
}

fn opening_frame(trimmed: &str, line: u32, start_byte: usize) -> Option<BlockFrame> {
    let header = trimmed.strip_suffix('{')?.trim_end();
    let name = header.split_whitespace().next()?;
    if name.starts_with('#') || name.contains('=') {
        return None;
    }
    let label = quoted_label(header.get(name.len()..).unwrap_or_default());
    let selection_start = start_byte;
    let selection_end = start_byte.saturating_add(header.len());
    Some(BlockFrame {
        kind: block_kind(name),
        name: name.to_owned(),
        label,
        start_line: line,
        start_byte,
        selection_start,
        selection_end,
    })
}

fn quoted_label(tail: &str) -> Option<String> {
    let tail = tail.trim();
    if !tail.starts_with('"') {
        return None;
    }
    serde_json::from_str::<String>(tail).ok()
}

const fn block_kind(name: &str) -> BlockKind {
    match name.as_bytes() {
        b"document" => BlockKind::Document,
        b"unit" => BlockKind::Unit,
        b"target" => BlockKind::Target,
        b"plural" => BlockKind::Plural,
        b"meta" => BlockKind::Meta,
        b"comment" => BlockKind::Comment,
        b"origin" => BlockKind::Origin,
        b"previous_context" | b"next_context" => BlockKind::AdjacentContext,
        b"tags" => BlockKind::Tags,
        b"source_parts" => BlockKind::SourceParts,
        b"target_parts" => BlockKind::TargetParts,
        b"parts" => BlockKind::Parts,
        b"source_tag" | b"target_tag" | b"tag" => BlockKind::Tag,
        _ => BlockKind::Other,
    }
}

fn context_at(text: &str, line_index: &LineIndex, offset: usize) -> Vec<BlockFrame> {
    let mut stack = Vec::new();
    let mut suppressed_depth = 0_usize;
    for line_number in 0..line_index.line_count() {
        let Some((line_start, line_end)) = line_index.line_bounds(text, line_number) else {
            continue;
        };
        if line_start >= offset {
            break;
        }
        let Some(full_end) = line_index.line_full_end(line_number) else {
            break;
        };
        let fragment_end = offset.min(line_end);
        let Some(line) = text.get(line_start..fragment_end) else {
            break;
        };
        let leading_trimmed = line.trim_start();
        let indentation = line.len().saturating_sub(leading_trimmed.len());
        let trimmed = leading_trimmed.trim_end();
        if trimmed == "}" {
            if suppressed_depth == 0 {
                stack.pop();
            } else {
                suppressed_depth -= 1;
            }
        } else if offset >= full_end {
            let frame = opening_frame(
                trimmed,
                u32::try_from(line_number).unwrap_or(u32::MAX),
                line_start.saturating_add(indentation),
            );
            if let Some(frame) = frame {
                if stack.len() < 32 {
                    stack.push(frame);
                } else {
                    suppressed_depth += 1;
                }
            }
        }
    }
    stack
}

#[cfg(test)]
mod tests {
    use std::str::FromStr;
    use std::time::{Duration, Instant};

    use lokit_format::{Data, Tags, TieData, TieType};
    use tower_lsp_server::ls_types::Range;

    use super::*;

    const SAMPLE: &str = "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"hello\" {\n  source = \"Hello\"\n  target \"fr\" {\n    text = \"Bonjour\"\n  }\n}\n";

    #[test]
    fn offers_contextual_fields_and_enum_values() {
        let root_index = LineIndex::new("");
        let root_items = completions(
            "",
            &root_index,
            Position::new(0, 0),
            PositionEncoding::Utf16,
            None,
            None,
        );
        assert!(root_items.items.iter().any(|item| item.label == "@lokit 1"));
        assert!(root_items.items.iter().any(|item| item.label == "document"));

        let unit_position = Position::new(5, 2);
        let sample_index = LineIndex::new(SAMPLE);
        let unit_items = completions(
            SAMPLE,
            &sample_index,
            unit_position,
            PositionEncoding::Utf16,
            None,
            None,
        );
        assert!(unit_items.items.iter().any(|item| item.label == "source"));
        assert!(unit_items.items.iter().any(|item| {
            item.label == "plural" && item.insert_text.as_deref() == Some("plural {\n  }")
        }));

        let status = "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n  status = \n}\n";
        let status_items = completions(
            status,
            &LineIndex::new(status),
            Position::new(6, 11),
            PositionEncoding::Utf16,
            None,
            None,
        );
        assert!(
            status_items
                .items
                .iter()
                .any(|item| item.label == "approved")
        );
        assert!(!status_items.items.iter().any(|item| item.label == "source"));
    }

    #[test]
    fn offers_tag_map_keys_for_code_references() {
        let mut structure = BaseStructure::new("en");
        let mut unit = Data::new("Hello");
        let mut tags = Tags::default();
        tags.source_tag_map
            .push(("open".to_owned(), TieData::new("b1", TieType::StrongOpen)));
        unit.tags = Some(tags);
        structure.data.push(("hello".to_owned(), unit));
        let text = "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"hello\" {\n  source = \"Hello\"\n  tags {\n    source_parts {\n      code = \n    }\n  }\n}\n";
        let items = completions(
            text,
            &LineIndex::new(text),
            Position::new(8, 13),
            PositionEncoding::Utf16,
            Some(&structure),
            None,
        );
        assert!(items.items.iter().any(|item| item.label == "open"));
    }

    #[test]
    fn completes_tolerantly_at_invalid_locale_and_code_edit_points() {
        let locale_text = "@lokit 1\ndocument {\n  source_locale = \"en\"\n  target_locales = [\"fr\", \"de\"]\n}\nunit \"hello\" {\n  source = \"Hello\"\n  target \"f\n}\n";
        let locale_index = LineIndex::new(locale_text);
        let locale_items = completions(
            locale_text,
            &locale_index,
            Position::new(7, 11),
            PositionEncoding::Utf16,
            None,
            None,
        );
        let french = locale_items.items.iter().find(|item| item.label == "fr");
        assert!(french.is_some());
        if let Some(french) = french {
            assert_eq!(
                french.text_edit,
                Some(CompletionTextEdit::Edit(TextEdit::new(
                    Range::new(Position::new(7, 9), Position::new(7, 11)),
                    "\"fr\"".to_owned(),
                )))
            );
            assert_eq!(french.kind, Some(CompletionItemKind::VALUE));
        }

        let code_text = "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"hello\" {\n  source = \"Hello\"\n  tags {\n    source_tag \"open\" {\n      id = \"b1\"\n      type = strong.open\n    }\n    source_parts {\n      code = \"op\n    }\n  }\n}\n";
        let code_items = completions(
            code_text,
            &LineIndex::new(code_text),
            Position::new(12, 16),
            PositionEncoding::Utf16,
            None,
            None,
        );
        let open = code_items.items.iter().find(|item| item.label == "open");
        assert!(open.is_some());
        if let Some(open) = open {
            assert_eq!(
                open.text_edit,
                Some(CompletionTextEdit::Edit(TextEdit::new(
                    Range::new(Position::new(12, 13), Position::new(12, 16)),
                    "\"open\"".to_owned(),
                )))
            );
        }
    }

    #[test]
    fn completion_edits_replace_auto_closed_quotes_without_consuming_suffixes() {
        let auto_closed = "@lokit 1\ndocument {\n  source_locale = \"en\"\n  target_locale = \"fr\"\n}\nunit \"u\" {\n  source = \"s\"\n  target \"\"\n}\n";
        let completion = completions(
            auto_closed,
            &LineIndex::new(auto_closed),
            Position::new(7, 10),
            PositionEncoding::Utf16,
            None,
            None,
        );
        let french = completion.items.iter().find(|item| item.label == "fr");
        assert!(french.is_some());
        if let Some(french) = french {
            assert_eq!(
                french.text_edit,
                Some(CompletionTextEdit::Edit(TextEdit::new(
                    Range::new(Position::new(7, 9), Position::new(7, 11)),
                    "\"fr\"".to_owned(),
                )))
            );
        }

        let completed_header = auto_closed.replacen("target \"\"", "target \"fr\" {", 1);
        let completion = completions(
            &completed_header,
            &LineIndex::new(&completed_header),
            Position::new(7, 15),
            PositionEncoding::Utf16,
            None,
            None,
        );
        let french = completion.items.iter().find(|item| item.label == "fr");
        assert!(french.is_none());
        for item in completion.items {
            if let Some(CompletionTextEdit::Edit(edit)) = item.text_edit {
                assert!(edit.range.start <= Position::new(7, 15));
                assert!(edit.range.end >= Position::new(7, 15));
            }
        }
    }

    #[test]
    fn filters_before_capping_completion_candidates() {
        let mut locales: Vec<String> = (0..160).map(|index| format!("locale-{index:03}")).collect();
        locales.push("zz-last".to_owned());
        let encoded = serde_json::to_string(&locales);
        assert!(encoded.is_ok());
        let encoded = encoded.unwrap_or_default();
        let text = format!(
            "@lokit 1\ndocument {{\n  source_locale = \"en\"\n  target_locales = {encoded}\n}}\nunit \"u\" {{\n  source = \"s\"\n  target \n}}\n"
        );
        let index = LineIndex::new(&text);
        let all = completions(
            &text,
            &index,
            Position::new(7, 9),
            PositionEncoding::Utf16,
            None,
            None,
        );
        assert_eq!(all.items.len(), MAX_COMPLETIONS);
        assert!(all.is_incomplete);
        assert!(!all.items.iter().any(|item| item.label == "zz-last"));

        let filtered_text = text.replacen("  target \n", "  target zz\n", 1);
        let filtered = completions(
            &filtered_text,
            &LineIndex::new(&filtered_text),
            Position::new(7, 11),
            PositionEncoding::Utf16,
            None,
            None,
        );
        assert!(!filtered.is_incomplete);
        assert_eq!(filtered.items.len(), 1);
        assert_eq!(filtered.items[0].label, "zz-last");
    }

    #[test]
    fn suppresses_generic_completion_in_comments_and_strings() {
        let comment = "@lokit 1\n# source\n";
        let comment_items = completions(
            comment,
            &LineIndex::new(comment),
            Position::new(1, 8),
            PositionEncoding::Utf16,
            None,
            None,
        );
        assert!(comment_items.items.is_empty());

        let string = "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"target \"\n}\n";
        let string_items = completions(
            string,
            &LineIndex::new(string),
            Position::new(5, 19),
            PositionEncoding::Utf16,
            None,
            None,
        );
        assert!(string_items.items.is_empty());
    }

    #[test]
    fn returns_schema_hover_with_a_precise_range() {
        let result = hover(
            SAMPLE,
            &LineIndex::new(SAMPLE),
            Position::new(2, 5),
            PositionEncoding::Utf16,
            MarkupKind::Markdown,
        );
        assert!(result.is_some());
        if let Some(result) = result {
            assert_eq!(
                result.range,
                Some(Range::new(Position::new(2, 2), Position::new(2, 15)))
            );
            match result.contents {
                HoverContents::Markup(markup) => assert!(markup.value.contains("source locale")),
                HoverContents::Scalar(_) | HoverContents::Array(_) => {
                    unreachable!("hover helper always returns markup")
                }
            }
        }
        let translation = "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"approved\"\n}\n";
        assert!(
            hover(
                translation,
                &LineIndex::new(translation),
                Position::new(5, 13),
                PositionEncoding::Utf16,
                MarkupKind::PlainText,
            )
            .is_none()
        );
    }

    #[test]
    fn builds_hierarchical_symbols() -> Result<(), Box<dyn std::error::Error>> {
        let uri = Uri::from_str("file:///workspace/sample.lokit")?;
        let cancelled = AtomicBool::new(false);
        let response = document_symbols(
            SAMPLE,
            &LineIndex::new(SAMPLE),
            PositionEncoding::Utf16,
            &uri,
            true,
            None,
            &cancelled,
        )
        .ok_or_else(|| std::io::Error::other("symbol extraction was cancelled"))?;
        let DocumentSymbolResponse::Nested(symbols) = response else {
            return Err(std::io::Error::other("expected hierarchical symbols").into());
        };
        assert_eq!(symbols.len(), 2);
        assert_eq!(symbols[0].name, "document");
        assert_eq!(symbols[1].name, "unit hello");
        let children = symbols[1].children.as_deref().unwrap_or_default();
        assert_eq!(children.len(), 1);
        assert_eq!(children[0].name, "target fr");
        assert_eq!(children[0].kind, SymbolKind::CLASS);
        Ok(())
    }

    #[test]
    fn returns_nested_folding_ranges() {
        let cancelled = AtomicBool::new(false);
        let ranges = folding_ranges(
            SAMPLE,
            &LineIndex::new(SAMPLE),
            MAX_FOLDING_RANGES,
            true,
            &cancelled,
        )
        .unwrap_or_default();
        assert_eq!(ranges.len(), 3);
        assert!(
            ranges
                .iter()
                .any(|range| range.start_line == 4 && range.end_line == 9)
        );
        assert!(
            ranges
                .iter()
                .any(|range| range.start_line == 6 && range.end_line == 8)
        );
    }

    #[test]
    fn symbol_and_folding_scans_honor_document_cancellation()
    -> Result<(), Box<dyn std::error::Error>> {
        let uri = Uri::from_str("file:///workspace/cancelled.lokit")?;
        let cancelled = AtomicBool::new(true);
        assert!(
            document_symbols(
                SAMPLE,
                &LineIndex::new(SAMPLE),
                PositionEncoding::Utf16,
                &uri,
                true,
                None,
                &cancelled,
            )
            .is_none()
        );
        assert!(
            folding_ranges(
                SAMPLE,
                &LineIndex::new(SAMPLE),
                MAX_FOLDING_RANGES,
                true,
                &cancelled,
            )
            .is_none()
        );
        Ok(())
    }

    #[test]
    fn scanners_accept_trailing_spaces_bare_cr_and_caps() -> Result<(), Box<dyn std::error::Error>>
    {
        let text = "document {   \r}   \runit \"u\" {   \r  target \"fr\" {   \r  }   \r}   \r";
        let index = LineIndex::new(text);
        let cancelled = AtomicBool::new(false);
        let blocks = scan_blocks(text, &index, 1, &cancelled).unwrap_or_default();
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0].frame.name, "document");
        let nested = "unit \"u\" {   \r  target \"fr\" {   \r  }   \r}   \r";
        let nested_blocks =
            scan_blocks(nested, &LineIndex::new(nested), 1, &cancelled).unwrap_or_default();
        assert_eq!(nested_blocks.len(), 1);
        assert_eq!(nested_blocks[0].frame.name, "unit");

        let uri = Uri::from_str("file:///workspace/spaced.lokit")?;
        let response = document_symbols(
            text,
            &index,
            PositionEncoding::Utf16,
            &uri,
            true,
            None,
            &cancelled,
        )
        .ok_or_else(|| std::io::Error::other("symbol extraction was cancelled"))?;
        let DocumentSymbolResponse::Nested(symbols) = response else {
            return Err(std::io::Error::other("expected hierarchical symbols").into());
        };
        assert_eq!(symbols.len(), 2);
        assert_eq!(symbols[1].children.as_deref().unwrap_or_default().len(), 1);

        let folds = folding_ranges(text, &index, 1, false, &cancelled).unwrap_or_default();
        assert_eq!(folds.len(), 1);
        assert!(folds[0].collapsed_text.is_none());
        Ok(())
    }

    #[test]
    fn symbols_scale_on_an_eight_megabyte_document() -> Result<(), Box<dyn std::error::Error>> {
        let padding = "x".repeat(8_100);
        let mut text = String::with_capacity(8 * 1024 * 1024);
        text.push_str("@lokit 1\ndocument {\n  source_locale = \"en\"\n}\n");
        for unit in 0..1_000 {
            text.push_str("unit \"");
            text.push_str(&unit.to_string());
            text.push_str("\" {\n  source = \"");
            text.push_str(&padding);
            text.push_str("\"\n}\n");
        }
        assert!(text.len() >= 8_000_000);
        let index = LineIndex::new(&text);
        let uri = Uri::from_str("file:///workspace/large.lokit")?;
        let cancelled = AtomicBool::new(false);
        let started = Instant::now();
        let response = document_symbols(
            &text,
            &index,
            PositionEncoding::Utf16,
            &uri,
            true,
            None,
            &cancelled,
        )
        .ok_or_else(|| std::io::Error::other("symbol extraction was cancelled"))?;
        let elapsed = started.elapsed();
        let DocumentSymbolResponse::Nested(symbols) = response else {
            return Err(std::io::Error::other("expected hierarchical symbols").into());
        };
        assert_eq!(symbols.len(), 1_001);
        assert!(
            elapsed < Duration::from_secs(2),
            "indexed symbol extraction took {elapsed:?}"
        );
        Ok(())
    }

    #[test]
    fn shared_parser_produces_syntax_and_semantic_diagnostics() {
        let cancelled = AtomicBool::new(false);
        let syntax_text = "not lokit\n";
        let syntax = analyze_document(
            syntax_text,
            &LineIndex::new(syntax_text),
            PositionEncoding::Utf16,
            &cancelled,
        );
        assert!(syntax.is_some());
        let Some(syntax) = syntax else {
            return;
        };
        assert!(syntax.structure.is_none());
        assert_eq!(syntax.diagnostics.len(), 1);
        assert_eq!(
            syntax.diagnostics[0].code,
            Some(NumberOrString::String("LKT005".to_owned()))
        );

        let semantic = "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"hello\"\n  tags {\n    source_tag \"defined\" {\n      id = \"b1\"\n      type = strong.open\n    }\n    source_parts {\n      code = \"missing\"\n    }\n  }\n}\n";
        let semantic = analyze_document(
            semantic,
            &LineIndex::new(semantic),
            PositionEncoding::Utf16,
            &cancelled,
        );
        assert!(semantic.is_some());
        let Some(semantic) = semantic else {
            return;
        };
        assert!(semantic.structure.is_some());
        assert!(semantic.diagnostics.iter().any(|diagnostic| {
            diagnostic.code == Some(NumberOrString::String("LKV001".to_owned()))
        }));
    }

    #[test]
    fn streamed_analysis_preserves_duplicate_validation_and_cancellation_results() {
        let duplicate = concat!(
            "@lokit 1\n",
            "document {\n  source_locale = \"en\"\n}\n",
            "unit \"same\" {\n  source = \"one\"\n}\n",
            "unit \"same\" {\n  source = \"two\"\n}\n",
        );
        let active = AtomicBool::new(false);
        let analysis = analyze_document(
            duplicate,
            &LineIndex::new(duplicate),
            PositionEncoding::Utf16,
            &active,
        );
        let Some(analysis) = analysis else {
            return;
        };
        assert!(analysis.structure.is_some());
        assert_eq!(analysis.diagnostics.len(), 1);
        assert_eq!(
            analysis.diagnostics[0].code,
            Some(NumberOrString::String("LKV008".to_owned()))
        );
        assert_eq!(analysis.diagnostics[0].range.start.line, 7);

        let cancelled = AtomicBool::new(true);
        assert!(
            analyze_document(
                SAMPLE,
                &LineIndex::new(SAMPLE),
                PositionEncoding::Utf16,
                &cancelled,
            )
            .is_none()
        );
    }

    #[test]
    fn canonical_formatting_uses_the_shared_writer() {
        let cancelled = AtomicBool::new(false);
        let analysis = analyze_document(
            SAMPLE,
            &LineIndex::new(SAMPLE),
            PositionEncoding::Utf16,
            &cancelled,
        );
        assert!(analysis.is_some());
        let Some(analysis) = analysis else {
            return;
        };
        assert!(analysis.structure.is_some());
        if let Some(structure) = analysis.structure {
            let canonical = canonical_text(SAMPLE, &structure);
            assert!(canonical.is_ok());
            if let Ok(canonical) = canonical {
                assert!(canonical.contains("}\n\nunit \"hello\""));
                assert!(canonical.ends_with('\n'));
            }
        }
        let commented = "# keep me\n@lokit 1\ndocument {\n  source_locale = \"en\"\n}\n";
        let formatted = parse_and_canonical_text(commented);
        assert!(formatted.is_ok());
        assert!(
            formatted
                .ok()
                .flatten()
                .is_some_and(|text| text.starts_with("# keep me\n@lokit 1\n"))
        );
    }
}
