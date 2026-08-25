use std::collections::HashSet;
use std::error::Error;
use std::fmt;
use std::io::{self, Write};

use crate::id_registry::BoundedIdRegistry;
use crate::model::{
    AdjacentContext, BaseStructure, Comment, Data, Meta, Origin, Plural, SegmentPart, Tags,
    TargetData, TargetTags, TieData, TranslationStatus,
};
use crate::{MAGIC, MAX_LINE_BYTES};

#[derive(Debug)]
pub enum WriteError {
    Io(io::Error),
    DuplicateKey {
        context: String,
        key: String,
    },
    LineTooLong {
        length: usize,
        maximum: usize,
    },
    InvalidState {
        operation: &'static str,
        state: &'static str,
    },
}

impl fmt::Display for WriteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "failed to write Lokit document: {error}"),
            Self::DuplicateKey { context, key } => {
                write!(formatter, "duplicate key {key:?} in {context}")
            }
            Self::LineTooLong { length, maximum } => write!(
                formatter,
                "canonical line is {length} bytes; maximum is {maximum} bytes"
            ),
            Self::InvalidState { operation, state } => {
                write!(formatter, "cannot {operation} while writer is {state}")
            }
        }
    }
}

impl Error for WriteError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::DuplicateKey { .. } | Self::LineTooLong { .. } | Self::InvalidState { .. } => {
                None
            }
        }
    }
}

impl From<io::Error> for WriteError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// Write one canonical Lokit document to `writer`.
///
/// The function validates all ordered-map keys before emitting bytes, so a
/// duplicate-key failure never leaves a partially written document.
pub fn write_document<W: Write>(
    writer: &mut W,
    document: &BaseStructure,
) -> Result<(), WriteError> {
    ensure_unique_keys(document)?;
    CanonicalWriter::new(writer).write_document_prevalidated(document)
}

/// Return the canonical UTF-8 representation of `document`.
pub fn format_source(document: &BaseStructure) -> Result<String, WriteError> {
    let mut bytes = Vec::new();
    write_document(&mut bytes, document)?;
    String::from_utf8(bytes).map_err(|error| {
        WriteError::Io(io::Error::new(
            io::ErrorKind::InvalidData,
            error.utf8_error(),
        ))
    })
}

/// Return the canonical UTF-8 representation while preserving every full-line
/// source comment in its original order and with its original line content.
///
/// Comments are anchored by the number of significant source lines that
/// precede them. This keeps formatting deterministic and idempotent even when
/// canonical field ordering changes. Canonical output still uses LF line
/// endings; the bytes within each comment line are retained exactly.
pub fn format_source_preserving_comments(
    source: &str,
    document: &BaseStructure,
) -> Result<String, WriteError> {
    let canonical = format_source(document)?;
    let comments = source_comments(source);
    if comments.is_empty() {
        return Ok(canonical);
    }

    let comment_bytes = comments
        .iter()
        .map(|comment| comment.text.len().saturating_add(1))
        .sum::<usize>();
    let mut output = String::with_capacity(canonical.len().saturating_add(comment_bytes));
    let mut next_comment = 0_usize;
    let mut significant_lines = 0_usize;

    for line in canonical.split_inclusive('\n') {
        let content = line.strip_suffix('\n').unwrap_or(line);
        if is_significant_line(content) {
            while comments
                .get(next_comment)
                .is_some_and(|comment| comment.preceding_significant_lines == significant_lines)
            {
                output.push_str(&comments[next_comment].text);
                output.push('\n');
                next_comment += 1;
            }
            significant_lines += 1;
        }
        output.push_str(line);
    }

    for comment in &comments[next_comment..] {
        output.push_str(&comment.text);
        output.push('\n');
    }
    Ok(output)
}

#[derive(Debug, Eq, PartialEq)]
struct SourceComment {
    preceding_significant_lines: usize,
    text: String,
}

fn source_comments(source: &str) -> Vec<SourceComment> {
    let mut comments = Vec::new();
    let mut significant_lines = 0_usize;
    for line in source.split_inclusive('\n') {
        let without_lf = line.strip_suffix('\n').unwrap_or(line);
        let content = without_lf.strip_suffix('\r').unwrap_or(without_lf);
        if is_full_line_comment(content) {
            comments.push(SourceComment {
                preceding_significant_lines: significant_lines,
                text: content.to_owned(),
            });
        } else if is_significant_line(content) {
            significant_lines += 1;
        }
    }
    comments
}

fn is_full_line_comment(line: &str) -> bool {
    line.bytes().find(|byte| !matches!(byte, b' ' | b'\t')) == Some(b'#')
}

fn is_significant_line(line: &str) -> bool {
    !line.trim_matches([' ', '\t']).is_empty() && !is_full_line_comment(line)
}

/// Stateful canonical writer useful for Python and streaming integrations.
pub struct CanonicalWriter<W: Write> {
    inner: W,
    state: WriterState,
    unit_ids: BoundedIdRegistry,
}

impl<W: Write> CanonicalWriter<W> {
    pub fn new(inner: W) -> Self {
        Self {
            inner,
            state: WriterState::New,
            unit_ids: BoundedIdRegistry::default(),
        }
    }

    pub fn get_ref(&self) -> &W {
        &self.inner
    }

    pub fn get_mut(&mut self) -> &mut W {
        &mut self.inner
    }

    pub fn into_inner(self) -> W {
        self.inner
    }

    pub const fn is_started(&self) -> bool {
        matches!(self.state, WriterState::Started)
    }

    pub const fn is_finished(&self) -> bool {
        matches!(self.state, WriterState::Finished)
    }

    /// Emit only the magic line and document metadata. `header.data` is not
    /// inspected or buffered; units are supplied individually with
    /// [`Self::write_unit`].
    pub fn start(&mut self, header: &BaseStructure) -> Result<(), WriteError> {
        self.require_state(WriterState::New, "start")?;
        ensure_header_unique(header)?;
        self.try_write(|writer| writer.write_header(header))?;
        self.state = WriterState::Started;
        Ok(())
    }

    /// Emit one unit, rejecting duplicate ids and duplicate ordered-map keys
    /// before writing any bytes for that unit.
    pub fn write_unit(&mut self, unit_id: &str, data: &Data) -> Result<(), WriteError> {
        self.require_state(WriterState::Started, "write a unit")?;
        if self.unit_id_is_registered(unit_id)? {
            return Err(WriteError::DuplicateKey {
                context: "document.units".to_owned(),
                key: unit_id.to_owned(),
            });
        }
        ensure_data_unique(data, &format!("unit {unit_id:?}"))?;
        if !self.register_unit_id(unit_id)? {
            return Err(WriteError::DuplicateKey {
                context: "document.units".to_owned(),
                key: unit_id.to_owned(),
            });
        }
        self.try_write(|writer| {
            writer.raw("\n")?;
            writer.write_data(unit_id, data)
        })
    }

    /// Flush the completed document. The header or last unit already provides
    /// the single canonical final newline, so finishing emits no extra bytes.
    pub fn finish(&mut self) -> Result<(), WriteError> {
        self.require_state(WriterState::Started, "finish")?;
        if let Err(error) = self.inner.flush() {
            self.state = WriterState::Poisoned;
            return Err(WriteError::Io(error));
        }
        self.state = WriterState::Finished;
        Ok(())
    }

    pub fn write_document(&mut self, document: &BaseStructure) -> Result<(), WriteError> {
        self.require_state(WriterState::New, "write a document")?;
        ensure_unique_keys(document)?;
        self.write_document_prevalidated(document)
    }

    fn write_document_prevalidated(&mut self, document: &BaseStructure) -> Result<(), WriteError> {
        self.try_write(|writer| writer.write_header(document))?;
        self.state = WriterState::Started;
        for (unit_id, data) in &document.data {
            self.try_write(|writer| {
                writer.raw("\n")?;
                writer.write_data(unit_id, data)
            })?;
        }
        self.finish()
    }

    fn unit_id_is_registered(&mut self, unit_id: &str) -> Result<bool, WriteError> {
        match self.unit_ids.contains(unit_id) {
            Ok(registered) => Ok(registered),
            Err(error) => {
                self.state = WriterState::Poisoned;
                Err(WriteError::Io(error))
            }
        }
    }

    fn register_unit_id(&mut self, unit_id: &str) -> Result<bool, WriteError> {
        match self.unit_ids.insert(unit_id) {
            Ok(inserted) => Ok(inserted),
            Err(error) => {
                self.state = WriterState::Poisoned;
                Err(WriteError::Io(error))
            }
        }
    }

    fn write_header(&mut self, document: &BaseStructure) -> Result<(), WriteError> {
        ensure_line_length(MAGIC.len())?;
        self.raw(MAGIC)?;
        self.raw("\n")?;
        self.open(0, "document", None)?;
        self.string(1, "source_locale", &document.source_locale)?;
        self.optional_string(1, "target_locale", document.target_locale.as_deref())?;
        if !document.target_locales.is_empty() {
            self.string_list(1, "target_locales", &document.target_locales)?;
        }
        if document.format_version != "0.1" {
            self.string(1, "format_version", &document.format_version)?;
        }
        if !document.export_origin.is_empty() {
            self.string(1, "export_origin", &document.export_origin)?;
        }
        if !document.export_timestamp.is_empty() {
            self.string(1, "export_timestamp", &document.export_timestamp)?;
        }
        self.optional_string(1, "source_language", document.source_language.as_deref())?;
        self.optional_string(1, "target_language", document.target_language.as_deref())?;
        if !document.target_languages.is_empty() {
            self.string_list(1, "target_languages", &document.target_languages)?;
        }
        self.string_map(1, "extension", &document.extensions)?;
        self.close(0)
    }

    fn require_state(
        &self,
        expected: WriterState,
        operation: &'static str,
    ) -> Result<(), WriteError> {
        if self.state == expected {
            Ok(())
        } else {
            Err(WriteError::InvalidState {
                operation,
                state: self.state.as_str(),
            })
        }
    }

    fn try_write(
        &mut self,
        operation: impl FnOnce(&mut Self) -> Result<(), WriteError>,
    ) -> Result<(), WriteError> {
        match operation(self) {
            Ok(()) => Ok(()),
            Err(error) => {
                self.state = WriterState::Poisoned;
                Err(error)
            }
        }
    }

    fn write_data(&mut self, unit_id: &str, data: &Data) -> Result<(), WriteError> {
        self.open(0, "unit", Some(unit_id))?;
        self.string(1, "source", &data.source)?;
        self.optional_string(1, "target", data.target.as_deref())?;
        for (locale, target) in &data.targets {
            self.write_target(locale, target)?;
        }
        if let Some(plural) = &data.plural {
            self.write_plural(1, plural)?;
        }
        if let Some(tags) = &data.tags {
            self.write_tags(1, tags)?;
        }
        if data.meta != Meta::default() {
            self.write_meta(1, &data.meta)?;
        }
        if data.status != TranslationStatus::Unknown {
            self.identifier(1, "status", data.status.as_str())?;
        }
        for comment in &data.comments {
            self.write_comment(1, comment)?;
        }
        if let Some(context) = &data.previous_context {
            self.write_context(1, "previous_context", context)?;
        }
        if let Some(context) = &data.next_context {
            self.write_context(1, "next_context", context)?;
        }
        self.string_map(1, "extension", &data.extensions)?;
        self.close(0)
    }

    fn write_target(&mut self, locale: &str, target: &TargetData) -> Result<(), WriteError> {
        self.open(1, "target", Some(locale))?;
        self.optional_string(2, "text", target.text.as_deref())?;
        if target.status != TranslationStatus::Unknown {
            self.identifier(2, "status", target.status.as_str())?;
        }
        if let Some(tags) = &target.tags {
            self.write_target_tags(2, tags)?;
        }
        if let Some(plural) = &target.plural {
            self.write_plural(2, plural)?;
        }
        if target.meta != Meta::default() {
            self.write_meta(2, &target.meta)?;
        }
        for comment in &target.comments {
            self.write_comment(2, comment)?;
        }
        self.string_map(2, "extension", &target.extensions)?;
        self.close(1)
    }

    fn write_plural(&mut self, depth: usize, plural: &Plural) -> Result<(), WriteError> {
        self.open(depth, "plural", None)?;
        self.string(depth + 1, "variant", &plural.variant)?;
        if let Some(count) = plural.count {
            self.integer(depth + 1, "count", count)?;
        }
        if let Some(category) = plural.category {
            self.identifier(depth + 1, "category", category.as_str())?;
        }
        self.string_map(depth + 1, "extension", &plural.extensions)?;
        self.close(depth)
    }

    fn write_meta(&mut self, depth: usize, meta: &Meta) -> Result<(), WriteError> {
        self.open(depth, "meta", None)?;
        self.optional_integer(depth + 1, "usage_count", meta.usage_count)?;
        self.optional_string(depth + 1, "last_used", meta.last_used.as_deref())?;
        self.optional_string(depth + 1, "first_used", meta.first_used.as_deref())?;
        self.optional_string(depth + 1, "created", meta.created.as_deref())?;
        self.optional_string(depth + 1, "updated", meta.updated.as_deref())?;
        self.optional_integer(depth + 1, "max_length", meta.max_length)?;
        self.optional_integer(depth + 1, "min_length", meta.min_length)?;
        self.string_map(depth + 1, "extension", &meta.extensions)?;
        self.close(depth)
    }

    fn write_comment(&mut self, depth: usize, comment: &Comment) -> Result<(), WriteError> {
        self.open(depth, "comment", None)?;
        self.string(depth + 1, "context", &comment.context)?;
        self.optional_string(depth + 1, "timestamp", comment.timestamp.as_deref())?;
        if let Some(origin) = &comment.origin {
            self.write_origin(depth + 1, origin)?;
        }
        self.optional_string(depth + 1, "context_key", comment.context_key.as_deref())?;
        self.string_map(depth + 1, "extension", &comment.extensions)?;
        self.close(depth)
    }

    fn write_origin(&mut self, depth: usize, origin: &Origin) -> Result<(), WriteError> {
        self.open(depth, "origin", None)?;
        self.optional_string(depth + 1, "system", origin.system.as_deref())?;
        self.optional_string(depth + 1, "project", origin.project.as_deref())?;
        self.optional_string(depth + 1, "creator_id", origin.creator_id.as_deref())?;
        self.string_map(depth + 1, "extension", &origin.extensions)?;
        self.close(depth)
    }

    fn write_context(
        &mut self,
        depth: usize,
        name: &str,
        context: &AdjacentContext,
    ) -> Result<(), WriteError> {
        self.open(depth, name, None)?;
        self.optional_string(depth + 1, "unit_id", context.unit_id.as_deref())?;
        self.optional_string(depth + 1, "source", context.source.as_deref())?;
        self.optional_string(depth + 1, "target", context.target.as_deref())?;
        self.string_map(depth + 1, "extension", &context.extensions)?;
        self.close(depth)
    }

    fn write_tags(&mut self, depth: usize, tags: &Tags) -> Result<(), WriteError> {
        self.open(depth, "tags", None)?;
        for (key, tag) in &tags.source_tag_map {
            self.write_tie(depth + 1, "source_tag", key, tag)?;
        }
        for (key, tag) in &tags.target_tag_map {
            self.write_tie(depth + 1, "target_tag", key, tag)?;
        }
        if !tags.source_parts.is_empty() {
            self.write_parts(depth + 1, "source_parts", &tags.source_parts)?;
        }
        if !tags.target_parts.is_empty() {
            self.write_parts(depth + 1, "target_parts", &tags.target_parts)?;
        }
        self.close(depth)
    }

    fn write_target_tags(&mut self, depth: usize, tags: &TargetTags) -> Result<(), WriteError> {
        self.open(depth, "tags", None)?;
        for (key, tag) in &tags.tag_map {
            self.write_tie(depth + 1, "tag", key, tag)?;
        }
        if !tags.parts.is_empty() {
            self.write_parts(depth + 1, "parts", &tags.parts)?;
        }
        self.close(depth)
    }

    fn write_tie(
        &mut self,
        depth: usize,
        name: &str,
        key: &str,
        tie: &TieData,
    ) -> Result<(), WriteError> {
        self.open(depth, name, Some(key))?;
        self.string(depth + 1, "id", &tie.id)?;
        self.identifier(depth + 1, "type", tie.r#type.as_str())?;
        self.string_map(depth + 1, "attribute", &tie.attributes)?;
        if !tie.attribute_data.is_empty() {
            self.string(depth + 1, "attribute_data", &tie.attribute_data)?;
        }
        if tie.position != 0 {
            self.integer(depth + 1, "position", tie.position)?;
        }
        if tie.order != 0 {
            self.integer(depth + 1, "order", tie.order)?;
        }
        self.optional_string(depth + 1, "pair_id", tie.pair_id.as_deref())?;
        self.optional_string(depth + 1, "original_name", tie.original_name.as_deref())?;
        self.optional_string(depth + 1, "original_text", tie.original_text.as_deref())?;
        self.close(depth)
    }

    fn write_parts(
        &mut self,
        depth: usize,
        name: &str,
        parts: &[SegmentPart],
    ) -> Result<(), WriteError> {
        self.open(depth, name, None)?;
        for part in parts {
            match part {
                SegmentPart::Text(text) => self.string(depth + 1, "text", &text.value)?,
                SegmentPart::Code(code) => self.string(depth + 1, "code", &code.r#ref)?,
            }
        }
        self.close(depth)
    }

    fn raw(&mut self, value: &str) -> Result<(), WriteError> {
        self.inner.write_all(value.as_bytes()).map_err(Into::into)
    }

    fn indent(&mut self, depth: usize) -> Result<(), WriteError> {
        for _ in 0..depth {
            self.raw("  ")?;
        }
        Ok(())
    }

    fn open(&mut self, depth: usize, name: &str, key: Option<&str>) -> Result<(), WriteError> {
        let key_length = key.map_or(0, |value| 1 + quoted_length(value));
        ensure_line_length(
            depth
                .saturating_mul(2)
                .saturating_add(name.len())
                .saturating_add(key_length)
                .saturating_add(2),
        )?;
        self.indent(depth)?;
        self.raw(name)?;
        if let Some(key) = key {
            self.raw(" ")?;
            self.quoted(key)?;
        }
        self.raw(" {\n")
    }

    fn close(&mut self, depth: usize) -> Result<(), WriteError> {
        ensure_line_length(depth.saturating_mul(2).saturating_add(1))?;
        self.indent(depth)?;
        self.raw("}\n")
    }

    fn string(&mut self, depth: usize, name: &str, value: &str) -> Result<(), WriteError> {
        ensure_line_length(
            depth
                .saturating_mul(2)
                .saturating_add(name.len())
                .saturating_add(3)
                .saturating_add(quoted_length(value)),
        )?;
        self.indent(depth)?;
        self.raw(name)?;
        self.raw(" = ")?;
        self.quoted(value)?;
        self.raw("\n")
    }

    fn optional_string(
        &mut self,
        depth: usize,
        name: &str,
        value: Option<&str>,
    ) -> Result<(), WriteError> {
        if let Some(value) = value {
            self.string(depth, name, value)?;
        }
        Ok(())
    }

    fn identifier(&mut self, depth: usize, name: &str, value: &str) -> Result<(), WriteError> {
        ensure_line_length(
            depth
                .saturating_mul(2)
                .saturating_add(name.len())
                .saturating_add(3)
                .saturating_add(value.len()),
        )?;
        self.indent(depth)?;
        self.raw(name)?;
        self.raw(" = ")?;
        self.raw(value)?;
        self.raw("\n")
    }

    fn integer(&mut self, depth: usize, name: &str, value: i64) -> Result<(), WriteError> {
        let integer_length = decimal_length(value);
        ensure_line_length(
            depth
                .saturating_mul(2)
                .saturating_add(name.len())
                .saturating_add(3)
                .saturating_add(integer_length),
        )?;
        self.indent(depth)?;
        self.raw(name)?;
        self.raw(" = ")?;
        writeln!(self.inner, "{value}").map_err(Into::into)
    }

    fn optional_integer(
        &mut self,
        depth: usize,
        name: &str,
        value: Option<i64>,
    ) -> Result<(), WriteError> {
        if let Some(value) = value {
            self.integer(depth, name, value)?;
        }
        Ok(())
    }

    fn string_map(
        &mut self,
        depth: usize,
        name: &str,
        values: &[(String, String)],
    ) -> Result<(), WriteError> {
        for (key, value) in values {
            ensure_line_length(
                depth
                    .saturating_mul(2)
                    .saturating_add(name.len())
                    .saturating_add(1)
                    .saturating_add(quoted_length(key))
                    .saturating_add(3)
                    .saturating_add(quoted_length(value)),
            )?;
            self.indent(depth)?;
            self.raw(name)?;
            self.raw(" ")?;
            self.quoted(key)?;
            self.raw(" = ")?;
            self.quoted(value)?;
            self.raw("\n")?;
        }
        Ok(())
    }

    fn string_list(
        &mut self,
        depth: usize,
        name: &str,
        values: &[String],
    ) -> Result<(), WriteError> {
        let mut line_length = depth
            .saturating_mul(2)
            .saturating_add(name.len())
            .saturating_add(5);
        for (index, value) in values.iter().enumerate() {
            line_length = line_length.saturating_add(quoted_length(value));
            if index != 0 {
                line_length = line_length.saturating_add(2);
            }
        }
        ensure_line_length(line_length)?;
        self.indent(depth)?;
        self.raw(name)?;
        self.raw(" = [")?;
        for (index, value) in values.iter().enumerate() {
            if index != 0 {
                self.raw(", ")?;
            }
            self.quoted(value)?;
        }
        self.raw("]\n")
    }

    fn quoted(&mut self, value: &str) -> Result<(), WriteError> {
        self.raw("\"")?;
        let mut unescaped_start = 0;
        for (offset, character) in value.char_indices() {
            let escaped = match character {
                '"' => Some("\\\""),
                '\\' => Some("\\\\"),
                '\u{0008}' => Some("\\b"),
                '\u{000c}' => Some("\\f"),
                '\n' => Some("\\n"),
                '\r' => Some("\\r"),
                '\t' => Some("\\t"),
                _ => None,
            };
            if let Some(escaped) = escaped {
                self.raw(&value[unescaped_start..offset])?;
                self.raw(escaped)?;
                unescaped_start = offset + character.len_utf8();
            } else if character <= '\u{001f}' {
                self.raw(&value[unescaped_start..offset])?;
                write!(self.inner, "\\u{:04x}", u32::from(character))?;
                unescaped_start = offset + character.len_utf8();
            }
        }
        self.raw(&value[unescaped_start..])?;
        self.raw("\"")
    }
}

fn ensure_line_length(length: usize) -> Result<(), WriteError> {
    if length <= MAX_LINE_BYTES {
        Ok(())
    } else {
        Err(WriteError::LineTooLong {
            length,
            maximum: MAX_LINE_BYTES,
        })
    }
}

fn quoted_length(value: &str) -> usize {
    value.chars().fold(2_usize, |length, character| {
        let encoded = match character {
            '"' | '\\' | '\u{0008}' | '\u{000c}' | '\n' | '\r' | '\t' => 2,
            value if value <= '\u{001f}' => 6,
            value => value.len_utf8(),
        };
        length.saturating_add(encoded)
    })
}

fn decimal_length(value: i64) -> usize {
    if value == 0 {
        return 1;
    }
    let mut magnitude = value.unsigned_abs();
    let mut length = usize::from(value.is_negative());
    while magnitude != 0 {
        length += 1;
        magnitude /= 10;
    }
    length
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum WriterState {
    New,
    Started,
    Finished,
    Poisoned,
}

impl WriterState {
    const fn as_str(self) -> &'static str {
        match self {
            Self::New => "new",
            Self::Started => "started",
            Self::Finished => "finished",
            Self::Poisoned => "failed",
        }
    }
}

fn ensure_unique_keys(document: &BaseStructure) -> Result<(), WriteError> {
    ensure_header_unique(document)?;
    unique_pairs(&document.data, "document.units")?;
    for (unit_id, data) in &document.data {
        ensure_data_unique(data, &format!("unit {unit_id:?}"))?;
    }
    Ok(())
}

fn ensure_header_unique(document: &BaseStructure) -> Result<(), WriteError> {
    unique_pairs(&document.extensions, "document.extensions")?;
    Ok(())
}

fn ensure_data_unique(data: &Data, path: &str) -> Result<(), WriteError> {
    unique_pairs(&data.targets, &format!("{path}.targets"))?;
    unique_pairs(&data.extensions, &format!("{path}.extensions"))?;
    if let Some(plural) = &data.plural {
        unique_pairs(&plural.extensions, &format!("{path}.plural.extensions"))?;
    }
    check_meta(&data.meta, &format!("{path}.meta"))?;
    check_comments(&data.comments, &format!("{path}.comments"))?;
    if let Some(context) = &data.previous_context {
        unique_pairs(
            &context.extensions,
            &format!("{path}.previous_context.extensions"),
        )?;
    }
    if let Some(context) = &data.next_context {
        unique_pairs(
            &context.extensions,
            &format!("{path}.next_context.extensions"),
        )?;
    }
    if let Some(tags) = &data.tags {
        check_tags(tags, &format!("{path}.tags"))?;
    }
    for (locale, target) in &data.targets {
        let target_path = format!("{path}.targets[{locale:?}]");
        unique_pairs(&target.extensions, &format!("{target_path}.extensions"))?;
        check_meta(&target.meta, &format!("{target_path}.meta"))?;
        check_comments(&target.comments, &format!("{target_path}.comments"))?;
        if let Some(plural) = &target.plural {
            unique_pairs(
                &plural.extensions,
                &format!("{target_path}.plural.extensions"),
            )?;
        }
        if let Some(tags) = &target.tags {
            check_target_tags(tags, &format!("{target_path}.tags"))?;
        }
    }
    Ok(())
}

fn check_meta(meta: &Meta, path: &str) -> Result<(), WriteError> {
    unique_pairs(&meta.extensions, &format!("{path}.extensions"))
}

fn check_comments(comments: &[Comment], path: &str) -> Result<(), WriteError> {
    for (index, comment) in comments.iter().enumerate() {
        let comment_path = format!("{path}[{index}]");
        unique_pairs(&comment.extensions, &format!("{comment_path}.extensions"))?;
        if let Some(origin) = &comment.origin {
            unique_pairs(
                &origin.extensions,
                &format!("{comment_path}.origin.extensions"),
            )?;
        }
    }
    Ok(())
}

fn check_tags(tags: &Tags, path: &str) -> Result<(), WriteError> {
    unique_pairs(&tags.source_tag_map, &format!("{path}.source_tag_map"))?;
    unique_pairs(&tags.target_tag_map, &format!("{path}.target_tag_map"))?;
    for (key, tag) in &tags.source_tag_map {
        unique_pairs(
            &tag.attributes,
            &format!("{path}.source_tag_map[{key:?}].attributes"),
        )?;
    }
    for (key, tag) in &tags.target_tag_map {
        unique_pairs(
            &tag.attributes,
            &format!("{path}.target_tag_map[{key:?}].attributes"),
        )?;
    }
    Ok(())
}

fn check_target_tags(tags: &TargetTags, path: &str) -> Result<(), WriteError> {
    unique_pairs(&tags.tag_map, &format!("{path}.tag_map"))?;
    for (key, tag) in &tags.tag_map {
        unique_pairs(
            &tag.attributes,
            &format!("{path}.tag_map[{key:?}].attributes"),
        )?;
    }
    Ok(())
}

fn unique_pairs<T>(values: &[(String, T)], context: &str) -> Result<(), WriteError> {
    let mut keys = HashSet::with_capacity(values.len());
    for (key, _) in values {
        if !keys.insert(key.as_str()) {
            return Err(WriteError::DuplicateKey {
                context: context.to_owned(),
                key: key.clone(),
            });
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;

    use super::{CanonicalWriter, WriteError};
    use crate::id_registry::BoundedIdRegistry;
    use crate::{BaseStructure, Data};

    #[test]
    fn streaming_writer_rejects_duplicates_atomically() {
        let mut writer = CanonicalWriter::new(Vec::new());
        writer.unit_ids = BoundedIdRegistry::default();
        writer
            .start(&BaseStructure::new("en"))
            .expect("header should write");
        writer
            .write_unit("alpha", &Data::new("one"))
            .expect("first unit should write");
        writer
            .write_unit("beta", &Data::new("two"))
            .expect("second unit should write");
        let output_before_duplicate = writer.get_ref().clone();
        let error = writer
            .write_unit("alpha", &Data::new("duplicate"))
            .expect_err("duplicate unit ID should fail");
        match error {
            WriteError::DuplicateKey { context, key } => {
                assert_eq!(context, "document.units");
                assert_eq!(key, "alpha");
            }
            other => panic!("expected duplicate key error, got {other}"),
        }
        assert_eq!(writer.get_ref(), &output_before_duplicate);
        writer
            .finish()
            .expect("duplicate rejection should not poison the writer");
        assert!(writer.is_finished());
    }

    #[test]
    fn streaming_writer_maps_registry_io_errors_without_writing_unit_bytes() {
        let mut writer = CanonicalWriter::new(Vec::new());
        writer.unit_ids = BoundedIdRegistry::default();
        writer
            .start(&BaseStructure::new("en"))
            .expect("header should write");
        writer
            .write_unit("alpha", &Data::new("one"))
            .expect("first unit should write");
        writer
            .write_unit("beta", &Data::new("two"))
            .expect("second unit should write");
        let output_before_error = writer.get_ref().clone();
        writer
            .unit_ids
            .fail_next_operation(ErrorKind::PermissionDenied);
        let error = writer
            .write_unit("gamma", &Data::new("three"))
            .expect_err("registry I/O failure should stop writing");
        match error {
            WriteError::Io(error) => {
                assert_eq!(error.kind(), ErrorKind::PermissionDenied);
                assert_eq!(error.to_string(), "injected unit ID registry failure");
            }
            other => panic!("expected I/O error, got {other}"),
        }
        assert_eq!(writer.get_ref(), &output_before_error);
        assert!(!writer.is_started());
        assert!(!writer.is_finished());
        assert!(matches!(
            writer.finish(),
            Err(WriteError::InvalidState {
                state: "failed",
                ..
            })
        ));
    }
}
