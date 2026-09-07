use std::collections::{BTreeMap, HashMap, VecDeque};
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};

use lokit_format::id_registry::BoundedIdRegistry;
use lokit_format::{BaseStructure, Comment, Data, Plural, PluralCategory, TranslationStatus};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyModule, PyTuple};
use quick_xml::events::{BytesDecl, BytesEnd, BytesRef, BytesStart, BytesText, Event};
use quick_xml::reader::Reader as XmlReader;
use quick_xml::Writer;

use super::{
    attribute_value, base_language, canonical_locale, is_xml_1_0_chars, native_to_py_error,
    InterchangeFormat, Metadata, NativeError, NativeParser, NativeRecord, NativeResult, ParseMode,
    PrefixBatch, DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE, READ_CAPACITY,
};
use crate::lokit::{data_from_python, PythonClasses};

const PO_MSGID: &str = "po_msgid";
const PO_MSGCTXT: &str = "po_msgctxt";
const PO_MSGID_PLURAL: &str = "po_msgid_plural";
const PO_COMMENT_KIND: &str = "po_comment_kind";
const PO_PREVIOUS: &str = "po_previous";
const PO_ENTRY_INDEX: &str = "po_entry_index";
const PO_METADATA_JSON: &str = "po_metadata_json";
const LOKIT_UNIT_ID: &str = "lokit_unit_id";
const LOKIT_UNIT_ID_COMMENT_PREFIX: &str = "lokit-unit-id-v1:";
/// Maximum PO line content accepted by the native reader, excluding the line
/// terminator and an optional UTF-8 BOM on the first line.
pub(crate) const MAX_PO_LINE_BYTES: usize = 1024 * 1024;
/// Maximum cumulative raw input retained for one logical PO entry. The blank
/// separator line is not included.
pub(crate) const MAX_PO_ENTRY_BYTES: usize = 16 * 1024 * 1024;
const MAX_PO_LINE_TERMINATOR_BYTES: usize = 2;
const UTF8_BOM_BYTES: usize = 3;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PoImportMode {
    Auto,
    Gettext,
    Source,
    TargetAsSource,
}

impl PoImportMode {
    pub(crate) fn parse(value: &str) -> NativeResult<Self> {
        match value {
            "auto" => Ok(Self::Auto),
            "gettext" | "msgid_as_source" => Ok(Self::Gettext),
            "source" => Ok(Self::Source),
            "target_as_source" | "msgid_as_id" => Ok(Self::TargetAsSource),
            _ => Err(NativeError::Invalid(format!(
                "PO mode must be 'auto', 'gettext', 'source', or 'target_as_source', got {value:?}"
            ))),
        }
    }

    const fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Gettext => "gettext",
            Self::Source => "source",
            Self::TargetAsSource => "target_as_source",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
enum PoField {
    #[default]
    None,
    Context,
    Id,
    IdPlural,
    Translation,
    PluralTranslation(u32),
}

#[derive(Clone, Debug, Default)]
struct PoEntry {
    obsolete: bool,
    context: Option<String>,
    id: String,
    id_plural: String,
    translation: String,
    plural_translations: BTreeMap<u32, String>,
    flags: Vec<String>,
    extracted_comments: Vec<String>,
    translator_comments: Vec<String>,
    references: Vec<(String, String)>,
    previous: Vec<String>,
    line: usize,
}

impl PoEntry {
    fn has_fields(&self) -> bool {
        self.line != 0
    }
}

#[derive(Clone, Debug, Default)]
struct PoHeader {
    fields: Vec<(String, String)>,
    translator_comments: Vec<String>,
    extracted_comments: Vec<String>,
    flags: Vec<String>,
    previous: Vec<String>,
}

impl PoHeader {
    fn get(&self, key: &str) -> Option<&str> {
        self.fields
            .iter()
            .find_map(|(candidate, value)| (candidate == key).then_some(value.as_str()))
    }

    fn metadata_json(&self) -> NativeResult<String> {
        let fields = self
            .fields
            .iter()
            .cloned()
            .collect::<BTreeMap<String, String>>();
        serde_json::to_string(&fields)
            .map_err(|error| NativeError::Invalid(format!("cannot encode PO metadata: {error}")))
    }
}

struct PoParser {
    reader: BufReader<File>,
    path: PathBuf,
    line_number: usize,
    pending: Option<PoEntry>,
    pending_units: VecDeque<(String, Data)>,
    header: PoHeader,
    metadata: Metadata,
    mode: PoImportMode,
    next_entry_index: usize,
    used_unit_ids: BoundedIdRegistry,
    finished: bool,
    max_line_bytes: usize,
    max_entry_bytes: usize,
}

impl PoParser {
    fn open(
        path: &Path,
        source_locale: Option<String>,
        target_locale: Option<String>,
        requested_mode: PoImportMode,
    ) -> NativeResult<Self> {
        let file = File::open(path)?;
        let mut parser = Self {
            reader: BufReader::with_capacity(READ_CAPACITY, file),
            path: path.to_path_buf(),
            line_number: 0,
            pending: None,
            pending_units: VecDeque::new(),
            header: PoHeader::default(),
            metadata: Metadata::default(),
            mode: requested_mode,
            next_entry_index: 0,
            used_unit_ids: BoundedIdRegistry::default(),
            finished: false,
            max_line_bytes: MAX_PO_LINE_BYTES,
            max_entry_bytes: MAX_PO_ENTRY_BYTES,
        };
        parser.initialize(source_locale, target_locale, requested_mode)?;
        Ok(parser)
    }

    fn initialize(
        &mut self,
        source_locale: Option<String>,
        target_locale: Option<String>,
        requested_mode: PoImportMode,
    ) -> NativeResult<()> {
        let first = self.read_entry()?;
        if let Some(entry) = first {
            if entry.id.is_empty() {
                self.header = header_from_entry(&entry);
            } else {
                self.pending = Some(entry);
            }
        }
        self.mode = match requested_mode {
            PoImportMode::Auto
                if self
                    .path
                    .extension()
                    .and_then(std::ffi::OsStr::to_str)
                    .is_some_and(|value| value.eq_ignore_ascii_case("pot")) =>
            {
                PoImportMode::Source
            }
            PoImportMode::Auto => PoImportMode::Gettext,
            explicit => explicit,
        };

        let header_locale = self
            .header
            .get("Language")
            .filter(|value| !value.is_empty());
        let resolved_source = match self.mode {
            PoImportMode::TargetAsSource => source_locale
                .or_else(|| header_locale.map(str::to_owned))
                .unwrap_or_default(),
            _ => source_locale.unwrap_or_default(),
        };
        let resolved_target = match self.mode {
            PoImportMode::Gettext => target_locale.or_else(|| header_locale.map(str::to_owned)),
            _ => None,
        };
        self.metadata.version = "0.1".to_owned();
        self.metadata.source_locale =
            (!resolved_source.is_empty()).then_some(resolved_source.clone());
        self.metadata.source_language =
            (!resolved_source.is_empty()).then(|| base_language(&resolved_source));
        if let Some(locale) = resolved_target {
            self.metadata.target_locale = Some(locale.clone());
            self.metadata.target_locales.push(locale.clone());
            self.metadata.target_language = Some(base_language(&locale));
            self.metadata.target_languages.push(base_language(&locale));
        }
        self.metadata.export_origin = self
            .header
            .get("X-Generator")
            .unwrap_or_default()
            .to_owned();
        self.metadata
            .extensions
            .insert("input_format".to_owned(), "po".to_owned());
        self.metadata
            .extensions
            .insert("po_import_mode".to_owned(), self.mode.as_str().to_owned());
        if !self.header.fields.is_empty() {
            self.metadata
                .extensions
                .insert(PO_METADATA_JSON.to_owned(), self.header.metadata_json()?);
        }
        insert_joined_extension(
            &mut self.metadata.extensions,
            "po_header_translator_comments",
            &self.header.translator_comments,
        );
        insert_joined_extension(
            &mut self.metadata.extensions,
            "po_header_extracted_comments",
            &self.header.extracted_comments,
        );
        insert_joined_extension(
            &mut self.metadata.extensions,
            "po_header_flags",
            &self.header.flags,
        );
        insert_joined_extension(
            &mut self.metadata.extensions,
            "po_header_previous",
            &self.header.previous,
        );
        Ok(())
    }

    fn read_batch(&mut self, batch_size: usize) -> NativeResult<Vec<(String, Data)>> {
        let batch = self.read_batch_preserving_prefix(batch_size);
        match batch.error {
            Some(error) => Err(error),
            None => Ok(batch.records),
        }
    }

    fn read_batch_preserving_prefix(&mut self, batch_size: usize) -> PrefixBatch<(String, Data)> {
        let mut records = Vec::with_capacity(batch_size.min(DEFAULT_BATCH_SIZE));
        let mut error = None;
        while records.len() < batch_size {
            if let Some(record) = self.pending_units.pop_front() {
                records.push(record);
                continue;
            }
            let entry = if let Some(entry) = self.pending.take() {
                Some(entry)
            } else {
                match self.read_entry() {
                    Ok(entry) => entry,
                    Err(failure) => {
                        error = Some(failure);
                        break;
                    }
                }
            };
            let Some(entry) = entry else {
                break;
            };
            if entry.obsolete || entry.id.is_empty() {
                continue;
            }
            let index = self.next_entry_index;
            self.next_entry_index += 1;
            match self.entry_units(entry, index) {
                Ok(units) => self.pending_units = units.into(),
                Err(failure) => {
                    error = Some(failure);
                    break;
                }
            }
        }
        PrefixBatch {
            records,
            error,
            exhausted: self.is_exhausted(),
        }
    }

    fn is_exhausted(&self) -> bool {
        self.finished && self.pending.is_none() && self.pending_units.is_empty()
    }

    fn next_data_entry(&mut self) -> NativeResult<Option<(usize, PoEntry)>> {
        loop {
            let entry = if let Some(entry) = self.pending.take() {
                Some(entry)
            } else {
                self.read_entry()?
            };
            let Some(entry) = entry else {
                return Ok(None);
            };
            if entry.obsolete || entry.id.is_empty() {
                continue;
            }
            let index = self.next_entry_index;
            self.next_entry_index += 1;
            return Ok(Some((index, entry)));
        }
    }

    fn entry_units(
        &mut self,
        entry: PoEntry,
        entry_index: usize,
    ) -> NativeResult<Vec<(String, Data)>> {
        let base_id = self.unique_unit_id(po_unit_id(&entry, entry_index))?;
        let extensions = entry_extensions(&entry, entry_index);
        let comments = entry_comments(&entry);
        if entry.id_plural.is_empty() {
            let (source, target) = self.singular_text(&entry);
            let data = Data {
                source,
                target,
                status: entry_status(&entry, self.mode, 0),
                comments,
                extensions: extensions.into_iter().collect(),
                ..Data::new(String::new())
            };
            return Ok(vec![(base_id, data)]);
        }

        let mut indexes = entry
            .plural_translations
            .keys()
            .copied()
            .collect::<Vec<_>>();
        if !indexes.contains(&0) {
            indexes.insert(0, 0);
        }
        indexes.sort_unstable();
        indexes.dedup();
        indexes
            .into_iter()
            .map(|index| -> NativeResult<(String, Data)> {
                let target = entry
                    .plural_translations
                    .get(&index)
                    .cloned()
                    .unwrap_or_default();
                let source = match self.mode {
                    PoImportMode::TargetAsSource => {
                        if target.is_empty() {
                            if index == 0 {
                                entry.id.clone()
                            } else {
                                entry.id_plural.clone()
                            }
                        } else {
                            target.clone()
                        }
                    }
                    _ if index == 0 => entry.id.clone(),
                    _ => entry.id_plural.clone(),
                };
                let target = match self.mode {
                    PoImportMode::Gettext => (!target.is_empty()).then_some(target),
                    _ => None,
                };
                let mut unit_extensions = extensions.clone();
                unit_extensions.push(("gettext_index".to_owned(), index.to_string()));
                let data = Data {
                    source,
                    target,
                    plural: Some(Plural {
                        variant: entry.id_plural.clone(),
                        count: None,
                        category: po_plural_category(
                            match self.mode {
                                PoImportMode::Gettext => self.metadata.target_locale.as_deref(),
                                _ => self.metadata.source_locale.as_deref(),
                            },
                            index,
                            po_nplurals(&self.header),
                        ),
                        extensions: vec![("gettext_index".to_owned(), index.to_string())],
                    }),
                    status: entry_status(&entry, self.mode, index),
                    comments: if index == 0 {
                        comments.clone()
                    } else {
                        Vec::new()
                    },
                    extensions: unit_extensions,
                    ..Data::new(String::new())
                };
                let preferred_id = if index == 0 {
                    base_id.clone()
                } else {
                    format!("{base_id}[{index}]")
                };
                let unit_id = if index == 0 {
                    preferred_id
                } else {
                    self.unique_unit_id(preferred_id)?
                };
                Ok((unit_id, data))
            })
            .collect()
    }

    fn unique_unit_id(&mut self, preferred: String) -> NativeResult<String> {
        if self.used_unit_ids.insert(&preferred)? {
            return Ok(preferred);
        }
        loop {
            let suffix = self.used_unit_ids.next_suffix(&preferred)?;
            let candidate = format!("{preferred}#{suffix}");
            if self.used_unit_ids.insert(&candidate)? {
                return Ok(candidate);
            }
        }
    }

    fn singular_text(&self, entry: &PoEntry) -> (String, Option<String>) {
        match self.mode {
            PoImportMode::TargetAsSource => (
                if entry.translation.is_empty() {
                    entry.id.clone()
                } else {
                    entry.translation.clone()
                },
                None,
            ),
            PoImportMode::Gettext => (
                entry.id.clone(),
                (!entry.translation.is_empty()).then(|| entry.translation.clone()),
            ),
            PoImportMode::Auto | PoImportMode::Source => (entry.id.clone(), None),
        }
    }

    fn read_entry(&mut self) -> NativeResult<Option<PoEntry>> {
        if self.finished {
            return Ok(None);
        }
        let mut entry = PoEntry::default();
        let mut field = PoField::None;
        let mut entry_raw_bytes = 0usize;
        let mut entry_start_line = None;
        let mut bytes = Vec::with_capacity(256.min(self.max_line_bytes));
        loop {
            let read = self.read_physical_line(&mut bytes)?;
            if read == 0 {
                self.finished = true;
                return Ok(entry.has_fields().then_some(entry));
            }
            let line = std::str::from_utf8(&bytes).map_err(|error| {
                NativeError::Invalid(format!(
                    "{}:{}: PO input is not valid UTF-8: {error}",
                    self.path.display(),
                    self.line_number
                ))
            })?;
            if line.trim().is_empty() {
                if entry.has_fields() {
                    return Ok(Some(entry));
                }
                entry = PoEntry::default();
                field = PoField::None;
                entry_raw_bytes = 0;
                entry_start_line = None;
                continue;
            }
            let start_line = *entry_start_line.get_or_insert(self.line_number);
            entry_raw_bytes = entry_raw_bytes.saturating_add(read);
            if entry_raw_bytes > self.max_entry_bytes {
                return Err(NativeError::Invalid(format!(
                    "{}:{}: PO entry starting on line {start_line} exceeds the {}-byte cumulative raw-input limit",
                    self.path.display(),
                    self.line_number,
                    self.max_entry_bytes,
                )));
            }
            let mut content = line;
            let mut obsolete = false;
            if let Some(rest) = content.strip_prefix("#~") {
                obsolete = true;
                entry.obsolete = true;
                content = rest.trim_start();
                if content.is_empty() {
                    continue;
                }
            }
            if let Some(rest) = content.strip_prefix("#|") {
                entry.previous.push(rest.trim_start().to_owned());
                continue;
            }
            if let Some(rest) = content.strip_prefix("#.") {
                entry.extracted_comments.push(rest.trim_start().to_owned());
                continue;
            }
            if let Some(rest) = content.strip_prefix("#:") {
                entry.references.extend(parse_references(rest.trim()));
                continue;
            }
            if let Some(rest) = content.strip_prefix("#,") {
                entry.flags.extend(
                    rest.split(',')
                        .map(str::trim)
                        .filter(|value| !value.is_empty())
                        .map(str::to_owned),
                );
                continue;
            }
            if let Some(rest) = content.strip_prefix('#') {
                entry.translator_comments.push(rest.trim_start().to_owned());
                continue;
            }
            if let Some(value) = directive_value(content, "msgctxt") {
                entry.context = Some(decode_po_literal(value, &self.path, self.line_number)?);
                field = PoField::Context;
            } else if let Some(value) = directive_value(content, "msgid_plural") {
                entry.id_plural = decode_po_literal(value, &self.path, self.line_number)?;
                field = PoField::IdPlural;
            } else if let Some(value) = directive_value(content, "msgid") {
                entry.id = decode_po_literal(value, &self.path, self.line_number)?;
                entry.line = self.line_number;
                field = PoField::Id;
            } else if let Some((index, value)) = plural_directive(content) {
                entry.plural_translations.insert(
                    index,
                    decode_po_literal(value, &self.path, self.line_number)?,
                );
                field = PoField::PluralTranslation(index);
            } else if let Some(value) = directive_value(content, "msgstr") {
                entry.translation = decode_po_literal(value, &self.path, self.line_number)?;
                field = PoField::Translation;
            } else if content.trim_start().starts_with('"') {
                let value = decode_po_literal(content.trim(), &self.path, self.line_number)?;
                append_field(&mut entry, field, &value)?;
            } else if obsolete {
                continue;
            } else {
                return Err(NativeError::Invalid(format!(
                    "{}:{}: unsupported PO syntax: {line:?}",
                    self.path.display(),
                    self.line_number
                )));
            }
        }
    }

    fn read_physical_line(&mut self, bytes: &mut Vec<u8>) -> NativeResult<usize> {
        bytes.clear();
        let bom_allowance = usize::from(self.line_number == 0) * UTF8_BOM_BYTES;
        let read_limit = self
            .max_line_bytes
            .saturating_add(MAX_PO_LINE_TERMINATOR_BYTES)
            .saturating_add(bom_allowance);
        let read_limit = u64::try_from(read_limit).unwrap_or(u64::MAX);
        let read = self
            .reader
            .by_ref()
            .take(read_limit)
            .read_until(b'\n', bytes)?;
        if read == 0 {
            return Ok(0);
        }
        self.line_number += 1;
        if bytes.ends_with(b"\n") {
            bytes.pop();
        }
        if bytes.ends_with(b"\r") {
            bytes.pop();
        }
        if self.line_number == 1 && bytes.starts_with(&[0xEF, 0xBB, 0xBF]) {
            bytes.drain(..UTF8_BOM_BYTES);
        }
        if bytes.len() > self.max_line_bytes {
            return Err(NativeError::Invalid(format!(
                "{}:{}: PO physical line exceeds the {}-byte content limit",
                self.path.display(),
                self.line_number,
                self.max_line_bytes,
            )));
        }
        Ok(read)
    }
}

fn directive_value<'a>(line: &'a str, directive: &str) -> Option<&'a str> {
    line.strip_prefix(directive).and_then(|rest| {
        rest.starts_with(char::is_whitespace)
            .then(|| rest.trim_start())
    })
}

fn plural_directive(line: &str) -> Option<(u32, &str)> {
    let rest = line.strip_prefix("msgstr[")?;
    let close = rest.find(']')?;
    let index = rest[..close].parse().ok()?;
    let value = rest[close + 1..].trim_start();
    value.starts_with('"').then_some((index, value))
}

fn decode_po_literal(value: &str, path: &Path, line: usize) -> NativeResult<String> {
    if value.len() < 2 || !value.starts_with('"') || !value.ends_with('"') {
        return Err(NativeError::Invalid(format!(
            "{}:{line}: invalid PO string literal: {value:?}",
            path.display()
        )));
    }
    let source = &value[1..value.len() - 1];
    let mut output = String::with_capacity(source.len());
    let mut characters = source.chars().peekable();
    while let Some(character) = characters.next() {
        if character != '\\' {
            output.push(character);
            continue;
        }
        let escaped = characters.next().ok_or_else(|| {
            NativeError::Invalid(format!(
                "{}:{line}: PO string ends with an escape prefix",
                path.display()
            ))
        })?;
        match escaped {
            'a' => output.push('\u{7}'),
            'b' => output.push('\u{8}'),
            'f' => output.push('\u{C}'),
            'n' => output.push('\n'),
            'r' => output.push('\r'),
            't' => output.push('\t'),
            'v' => output.push('\u{B}'),
            '\\' => output.push('\\'),
            '"' => output.push('"'),
            '0'..='7' => {
                let mut digits = String::from(escaped);
                while digits.len() < 3
                    && characters
                        .peek()
                        .is_some_and(|value| matches!(value, '0'..='7'))
                {
                    digits.push(characters.next().expect("peeked octal digit exists"));
                }
                push_scalar(&mut output, &digits, 8, path, line)?;
            }
            'x' => {
                let mut digits = String::new();
                while characters
                    .peek()
                    .is_some_and(|value| value.is_ascii_hexdigit())
                {
                    digits.push(characters.next().expect("peeked hexadecimal digit exists"));
                }
                if digits.is_empty() {
                    return Err(NativeError::Invalid(format!(
                        "{}:{line}: PO hexadecimal escape has no digits",
                        path.display()
                    )));
                }
                push_scalar(&mut output, &digits, 16, path, line)?;
            }
            other => output.push(other),
        }
    }
    Ok(output)
}

fn push_scalar(
    output: &mut String,
    digits: &str,
    radix: u32,
    path: &Path,
    line: usize,
) -> NativeResult<()> {
    let value = u32::from_str_radix(digits, radix).map_err(|error| {
        NativeError::Invalid(format!(
            "{}:{line}: invalid PO escape: {error}",
            path.display()
        ))
    })?;
    let character = char::from_u32(value).ok_or_else(|| {
        NativeError::Invalid(format!(
            "{}:{line}: PO escape does not encode a Unicode scalar value: U+{value:04X}",
            path.display()
        ))
    })?;
    output.push(character);
    Ok(())
}

fn append_field(entry: &mut PoEntry, field: PoField, value: &str) -> NativeResult<()> {
    match field {
        PoField::Context => entry.context.get_or_insert_default().push_str(value),
        PoField::Id => entry.id.push_str(value),
        PoField::IdPlural => entry.id_plural.push_str(value),
        PoField::Translation => entry.translation.push_str(value),
        PoField::PluralTranslation(index) => entry
            .plural_translations
            .entry(index)
            .or_default()
            .push_str(value),
        PoField::None => {
            return Err(NativeError::Invalid(
                "PO continuation appears before a field".to_owned(),
            ));
        }
    }
    Ok(())
}

fn parse_references(value: &str) -> Vec<(String, String)> {
    value
        .split_whitespace()
        .map(|item| {
            let Some((path, line)) = item.rsplit_once(':') else {
                return (item.to_owned(), String::new());
            };
            if line.bytes().all(|byte| byte.is_ascii_digit()) {
                (path.to_owned(), line.to_owned())
            } else {
                (item.to_owned(), String::new())
            }
        })
        .collect()
}

fn header_from_entry(entry: &PoEntry) -> PoHeader {
    let mut fields = Vec::new();
    for line in entry.translation.lines() {
        if let Some((key, value)) = line.split_once(':') {
            fields.push((key.trim().to_owned(), value.trim().to_owned()));
        }
    }
    PoHeader {
        fields,
        translator_comments: entry.translator_comments.clone(),
        extracted_comments: entry.extracted_comments.clone(),
        flags: entry.flags.clone(),
        previous: entry.previous.clone(),
    }
}

fn insert_joined_extension(extensions: &mut HashMap<String, String>, key: &str, values: &[String]) {
    if !values.is_empty() {
        extensions.insert(key.to_owned(), values.join("\n"));
    }
}

fn po_unit_id(entry: &PoEntry, entry_index: usize) -> String {
    if let Some(unit_id) = entry
        .extracted_comments
        .iter()
        .find_map(|comment| decode_lokit_unit_id_comment(comment))
    {
        return unit_id;
    }
    if entry.context.is_none() && is_xml_1_0_chars(&entry.id) && !entry.id.is_empty() {
        return entry.id.clone();
    }
    format!("po-{entry_index}")
}

fn encode_lokit_unit_id_comment(unit_id: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(LOKIT_UNIT_ID_COMMENT_PREFIX.len() + unit_id.len() * 2);
    encoded.push_str(LOKIT_UNIT_ID_COMMENT_PREFIX);
    for byte in unit_id.bytes() {
        encoded.push(char::from(HEX[usize::from(byte >> 4)]));
        encoded.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    encoded
}

fn decode_lokit_unit_id_comment(comment: &str) -> Option<String> {
    let encoded = comment.strip_prefix(LOKIT_UNIT_ID_COMMENT_PREFIX)?;
    if encoded.is_empty() || encoded.len() % 2 != 0 {
        return None;
    }
    let mut decoded = Vec::with_capacity(encoded.len() / 2);
    for pair in encoded.as_bytes().chunks_exact(2) {
        let high = hex_nibble(pair[0])?;
        let low = hex_nibble(pair[1])?;
        decoded.push((high << 4) | low);
    }
    String::from_utf8(decoded)
        .ok()
        .filter(|unit_id| !unit_id.is_empty() && is_xml_1_0_chars(unit_id))
}

const fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn entry_extensions(entry: &PoEntry, entry_index: usize) -> Vec<(String, String)> {
    let mut extensions = vec![
        (PO_MSGID.to_owned(), entry.id.clone()),
        (PO_ENTRY_INDEX.to_owned(), entry_index.to_string()),
    ];
    if let Some(context) = entry.context.as_ref() {
        extensions.push((PO_MSGCTXT.to_owned(), context.clone()));
    }
    if !entry.id_plural.is_empty() {
        extensions.push((PO_MSGID_PLURAL.to_owned(), entry.id_plural.clone()));
    }
    if !entry.flags.is_empty() {
        let flags = entry
            .flags
            .iter()
            .filter(|flag| flag.as_str() != "fuzzy")
            .cloned()
            .collect::<Vec<_>>();
        if !flags.is_empty() {
            extensions.push(("flags".to_owned(), flags.join(", ")));
        }
    }
    if !entry.references.is_empty() {
        extensions.push((
            "references".to_owned(),
            entry
                .references
                .iter()
                .map(|(path, line)| {
                    if line.is_empty() {
                        path.clone()
                    } else {
                        format!("{path}:{line}")
                    }
                })
                .collect::<Vec<_>>()
                .join(", "),
        ));
    }
    if !entry.previous.is_empty() {
        extensions.push((PO_PREVIOUS.to_owned(), entry.previous.join("\n")));
    }
    if let Some(unit_id) = entry
        .extracted_comments
        .iter()
        .find_map(|comment| decode_lokit_unit_id_comment(comment))
    {
        extensions.push((LOKIT_UNIT_ID.to_owned(), unit_id));
    }
    extensions
}

fn entry_comments(entry: &PoEntry) -> Vec<Comment> {
    let mut comments = Vec::new();
    for value in &entry.translator_comments {
        let mut comment = Comment::new(value.clone());
        comment
            .extensions
            .push((PO_COMMENT_KIND.to_owned(), "translator".to_owned()));
        comments.push(comment);
    }
    for value in entry
        .extracted_comments
        .iter()
        .filter(|comment| decode_lokit_unit_id_comment(comment).is_none())
    {
        let mut comment = Comment::new(value.clone());
        comment.context_key = entry.context.clone();
        comment
            .extensions
            .push((PO_COMMENT_KIND.to_owned(), "extracted".to_owned()));
        comments.push(comment);
    }
    comments
}

fn po_plural_category(
    locale: Option<&str>,
    index: u32,
    nplurals: Option<u32>,
) -> Option<PluralCategory> {
    let locale = locale?;
    let language = locale
        .split(['-', '_'])
        .next()
        .unwrap_or(locale)
        .to_ascii_lowercase();
    let categories: &[PluralCategory] = match language.as_str() {
        "ar" => &[
            PluralCategory::Zero,
            PluralCategory::One,
            PluralCategory::Two,
            PluralCategory::Few,
            PluralCategory::Many,
            PluralCategory::Other,
        ],
        "he" => &[
            PluralCategory::One,
            PluralCategory::Two,
            PluralCategory::Other,
            PluralCategory::Other,
        ],
        "be" | "ru" | "uk" => &[
            PluralCategory::One,
            PluralCategory::Few,
            PluralCategory::Many,
        ],
        "pl" => &[
            PluralCategory::One,
            PluralCategory::Few,
            PluralCategory::Many,
        ],
        "cs" | "sk" => &[
            PluralCategory::One,
            PluralCategory::Few,
            PluralCategory::Other,
        ],
        "sl" => &[
            PluralCategory::One,
            PluralCategory::Two,
            PluralCategory::Few,
            PluralCategory::Other,
        ],
        "cy" => &[
            PluralCategory::Zero,
            PluralCategory::One,
            PluralCategory::Two,
            PluralCategory::Few,
            PluralCategory::Many,
            PluralCategory::Other,
        ],
        "ga" => &[
            PluralCategory::One,
            PluralCategory::Two,
            PluralCategory::Few,
            PluralCategory::Many,
            PluralCategory::Other,
        ],
        "lv" => &[
            PluralCategory::Zero,
            PluralCategory::One,
            PluralCategory::Other,
        ],
        "lt" => &[
            PluralCategory::One,
            PluralCategory::Few,
            PluralCategory::Other,
        ],
        "ro" => &[
            PluralCategory::One,
            PluralCategory::Few,
            PluralCategory::Other,
        ],
        "ja" | "ko" | "lo" | "my" | "th" | "vi" | "zh" => &[PluralCategory::Other],
        "ca" | "es" | "fr" | "it" | "pt" => &[
            PluralCategory::One,
            PluralCategory::Many,
            PluralCategory::Other,
        ],
        "af" | "bg" | "bn" | "da" | "de" | "el" | "en" | "et" | "eu" | "fi" | "fo" | "gl"
        | "gu" | "hi" | "hu" | "is" | "kn" | "mr" | "nb" | "nl" | "nn" | "no" | "pa" | "sv"
        | "sw" | "ta" | "te" | "tr" => &[PluralCategory::One, PluralCategory::Other],
        _ => return None,
    };
    if nplurals.is_some_and(|count| count as usize != categories.len()) {
        return (index == 0).then_some(categories[0]);
    }
    categories.get(index as usize).copied()
}

fn entry_status(entry: &PoEntry, mode: PoImportMode, index: u32) -> TranslationStatus {
    if mode == PoImportMode::Source || mode == PoImportMode::TargetAsSource {
        return TranslationStatus::New;
    }
    if entry.flags.iter().any(|flag| flag == "fuzzy") {
        return TranslationStatus::Draft;
    }
    let translated = if entry.id_plural.is_empty() {
        !entry.translation.is_empty()
    } else {
        entry
            .plural_translations
            .get(&index)
            .is_some_and(|value| !value.is_empty())
    };
    if translated {
        TranslationStatus::Translated
    } else {
        TranslationStatus::New
    }
}

#[pyclass(module = "lokit._interchange_rust")]
pub(crate) struct PoReader {
    parser: Option<PoParser>,
    final_metadata: Metadata,
    pending_error: Option<NativeError>,
    exhausted: bool,
}

#[pymethods]
impl PoReader {
    #[new]
    #[pyo3(signature = (path, source_locale=None, target_locale=None, mode="auto"))]
    fn new(
        py: Python<'_>,
        path: &str,
        source_locale: Option<String>,
        target_locale: Option<String>,
        mode: &str,
    ) -> PyResult<Self> {
        let mode = PoImportMode::parse(mode).map_err(native_to_py_error)?;
        let path = path.to_owned();
        let parser = py
            .detach(move || PoParser::open(Path::new(&path), source_locale, target_locale, mode))
            .map_err(native_to_py_error)?;
        let exhausted = parser.is_exhausted();
        let final_metadata = parser.metadata.clone();
        Ok(Self {
            parser: (!exhausted).then_some(parser),
            final_metadata,
            pending_error: None,
            exhausted,
        })
    }

    #[pyo3(signature = (batch_size=DEFAULT_BATCH_SIZE))]
    fn read_batch(&mut self, py: Python<'_>, batch_size: usize) -> PyResult<Vec<Py<PyTuple>>> {
        if batch_size == 0 || batch_size > MAX_BATCH_SIZE {
            return Err(PyValueError::new_err(format!(
                "batch_size must be between 1 and {MAX_BATCH_SIZE}"
            )));
        }
        if let Some(error) = self.pending_error.take() {
            return Err(native_to_py_error(error));
        }
        if self.exhausted {
            return Ok(Vec::new());
        }
        let PrefixBatch {
            records,
            error,
            exhausted,
        } = {
            let parser = self
                .parser
                .as_mut()
                .ok_or_else(|| PyRuntimeError::new_err("native PO reader is closed"))?;
            py.detach(|| parser.read_batch_preserving_prefix(batch_size))
        };
        if error.is_some() || exhausted {
            self.release_parser();
        }
        if let Some(error) = error {
            if records.is_empty() {
                return Err(native_to_py_error(error));
            }
            self.pending_error = Some(error);
        } else if exhausted {
            self.exhausted = true;
        }
        let classes = PythonClasses::import(py)?;
        records
            .into_iter()
            .map(|(unit_id, data)| {
                let unit_id = unit_id.into_pyobject(py)?.into_any();
                let data = classes.data(py, data)?;
                Ok(PyTuple::new(py, [unit_id, data])?.unbind())
            })
            .collect()
    }

    fn close(&mut self) {
        self.release_parser();
        self.pending_error = None;
        self.exhausted = false;
    }

    #[getter]
    fn closed(&self) -> bool {
        self.parser.is_none()
    }

    #[getter]
    fn source_locale(&self) -> String {
        self.metadata().source_locale.clone().unwrap_or_default()
    }

    #[getter]
    fn target_locale(&self) -> Option<String> {
        self.metadata().target_locale.clone()
    }

    #[getter]
    fn source_language(&self) -> Option<String> {
        self.metadata().source_language.clone()
    }

    #[getter]
    fn target_language(&self) -> Option<String> {
        self.metadata().target_language.clone()
    }

    #[getter]
    fn target_locales(&self) -> Vec<String> {
        self.metadata().target_locales.clone()
    }

    #[getter]
    fn target_languages(&self) -> Vec<String> {
        self.metadata().target_languages.clone()
    }

    #[getter]
    fn export_origin(&self) -> String {
        self.metadata().export_origin.clone()
    }

    #[getter]
    fn export_timestamp(&self) -> String {
        self.metadata().export_timestamp.clone()
    }

    #[getter]
    fn extensions(&self) -> HashMap<String, String> {
        self.metadata().extensions.clone()
    }
}

impl PoReader {
    fn release_parser(&mut self) {
        if let Some(parser) = self.parser.take() {
            self.final_metadata = parser.metadata;
        }
    }

    fn metadata(&self) -> &Metadata {
        self.parser
            .as_ref()
            .map_or(&self.final_metadata, |parser| &parser.metadata)
    }
}

#[pyfunction]
#[pyo3(signature = (path, source_locale=None, target_locale=None, mode="auto"))]
fn materialize_po(
    py: Python<'_>,
    path: &str,
    source_locale: Option<String>,
    target_locale: Option<String>,
    mode: &str,
) -> PyResult<Py<PyAny>> {
    let mode = PoImportMode::parse(mode).map_err(native_to_py_error)?;
    let path = path.to_owned();
    let mut parser = py
        .detach(move || PoParser::open(Path::new(&path), source_locale, target_locale, mode))
        .map_err(native_to_py_error)?;
    let classes = PythonClasses::import(py)?;
    let data = PyDict::new(py);
    loop {
        let records = py
            .detach(|| parser.read_batch(DEFAULT_BATCH_SIZE))
            .map_err(native_to_py_error)?;
        if records.is_empty() {
            break;
        }
        for (unit_id, unit) in records {
            data.set_item(unit_id, classes.data(py, unit)?)?;
        }
    }
    classes
        .base_structure_with_data(py, po_document_header(&parser.metadata), data)
        .map(Bound::unbind)
}

fn po_document_header(metadata: &Metadata) -> BaseStructure {
    BaseStructure {
        source_locale: metadata.source_locale.clone().unwrap_or_default(),
        target_locale: metadata.target_locale.clone(),
        data: Vec::new(),
        target_locales: metadata.target_locales.clone(),
        format_version: "0.1".to_owned(),
        export_origin: metadata.export_origin.clone(),
        export_timestamp: metadata.export_timestamp.clone(),
        source_language: metadata.source_language.clone(),
        target_language: metadata.target_language.clone(),
        target_languages: metadata.target_languages.clone(),
        extensions: metadata
            .extensions
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect(),
    }
}

pub(crate) fn convert_po_to_interchange(
    source_path: &Path,
    target_path: &Path,
    output_format: InterchangeFormat,
    source_locale: Option<String>,
    target_locale: Option<String>,
    mode: PoImportMode,
) -> NativeResult<usize> {
    let mut parser = PoParser::open(source_path, source_locale, target_locale, mode)?;
    validate_po_metadata_for_xml(&parser)?;
    let file = File::create(target_path)?;
    let stream = BufWriter::with_capacity(READ_CAPACITY, file);
    let mut writer = Writer::new(stream);
    writer.write_event(Event::Decl(BytesDecl::new("1.0", Some("UTF-8"), None)))?;
    match output_format {
        InterchangeFormat::Tmx => {
            write_po_tmx_start(&mut writer, &parser.header, &parser.metadata)?
        }
        InterchangeFormat::Xliff => {
            write_po_xliff_start(&mut writer, source_path, &parser.header, &parser.metadata)?
        }
    }
    let mut units = 0;
    while let Some((index, entry)) = parser.next_data_entry()? {
        validate_po_entry_for_xml(&entry)?;
        match output_format {
            InterchangeFormat::Tmx => {
                units +=
                    write_po_entry_tmx(&mut writer, &entry, index, &parser.metadata, parser.mode)?
            }
            InterchangeFormat::Xliff => {
                units += write_po_entry_xliff(
                    &mut writer,
                    &entry,
                    index,
                    &parser.metadata,
                    parser.mode,
                )?;
            }
        }
    }
    match output_format {
        InterchangeFormat::Tmx => {
            writer.write_event(Event::End(BytesEnd::new("body")))?;
            writer.write_event(Event::End(BytesEnd::new("tmx")))?;
        }
        InterchangeFormat::Xliff => {
            writer.write_event(Event::End(BytesEnd::new("body")))?;
            writer.write_event(Event::End(BytesEnd::new("file")))?;
            writer.write_event(Event::End(BytesEnd::new("xliff")))?;
        }
    }
    writer.get_mut().write_all(b"\n")?;
    writer.get_mut().flush()?;
    Ok(units)
}

#[derive(Clone, Debug)]
enum XmlPoField {
    Context(String),
    Note(String),
    Property(String),
}

#[derive(Clone, Debug)]
struct XmlPoCapture {
    element_name: Vec<u8>,
    field: XmlPoField,
    value: String,
}

#[derive(Clone, Debug, Default)]
struct InterchangePoDetails {
    header: bool,
    msgid: Option<String>,
    context: Option<String>,
    msgid_plural: Option<String>,
    plural_index: Option<u32>,
    entry_index: Option<String>,
    flags: Vec<String>,
    references: Vec<(String, String)>,
    previous: Vec<String>,
    translator_comments: Vec<String>,
    extracted_comments: Vec<String>,
    metadata_json: Option<String>,
    lokit_unit_id: Option<String>,
}

pub(crate) fn convert_interchange_to_po(
    source_path: &Path,
    target_path: &Path,
    input_format: InterchangeFormat,
    source_locale: Option<String>,
    target_locale: Option<String>,
    mode: ParseMode,
) -> NativeResult<Option<usize>> {
    let mut parser = NativeParser::open(
        source_path,
        input_format,
        source_locale,
        target_locale,
        mode,
    )?;
    let mut header = interchange_po_header(source_path, input_format, &parser.metadata)?;
    let file = File::create(target_path)?;
    let mut writer = BufWriter::with_capacity(READ_CAPACITY, file);
    let mut header_written = false;
    let mut pending: Option<(String, PoEntry)> = None;
    let mut entries = 0;

    loop {
        let records = parser.read_batch(DEFAULT_BATCH_SIZE)?;
        if records.is_empty() {
            break;
        }
        for record in records {
            let details = interchange_po_details(&record)?;
            if details.header {
                let value = selected_record_target(&record, &parser.metadata)?
                    .unwrap_or_else(|| record.source.clone());
                merge_header_text(&mut header, &value);
                merge_details_into_header(&mut header, &details)?;
                continue;
            }
            if !header_written {
                complete_interchange_header(&mut header, &parser.metadata);
                write_po_header(&mut writer, &header)?;
                header_written = true;
            }
            let target = selected_record_target(&record, &parser.metadata)?.unwrap_or_default();
            let (identity, plural_index, plural) = interchange_plural_identity(&record, &details);
            if plural && po_nplurals(&header).is_some_and(|count| plural_index >= count) {
                if let Some((pending_identity, entry)) = pending.as_mut() {
                    if pending_identity == &identity && entry.id_plural.is_empty() {
                        entry.id_plural = details
                            .msgid_plural
                            .clone()
                            .unwrap_or_else(|| record.source.clone());
                    }
                }
                continue;
            }
            if pending
                .as_ref()
                .is_some_and(|(pending_identity, _)| pending_identity != &identity)
            {
                let (_, entry) = pending.take().expect("pending PO entry exists");
                write_po_entry(&mut writer, &entry)?;
                entries += 1;
            }
            let (_, entry) = pending.get_or_insert_with(|| {
                (
                    identity,
                    po_entry_from_interchange(&record, &details, plural, plural_index),
                )
            });
            merge_interchange_record(entry, &record, &details, &target, plural, plural_index);
        }
    }

    if !header_written {
        complete_interchange_header(&mut header, &parser.metadata);
        write_po_header(&mut writer, &header)?;
    }
    if let Some((_, entry)) = pending {
        write_po_entry(&mut writer, &entry)?;
        entries += 1;
    }
    writer.flush()?;
    Ok(Some(entries))
}

fn interchange_po_header(
    path: &Path,
    format: InterchangeFormat,
    metadata: &Metadata,
) -> NativeResult<PoHeader> {
    let mut header = PoHeader::default();
    let xliff_properties = if format == InterchangeFormat::Xliff {
        Some(read_xliff_po_header_properties(path)?)
    } else {
        None
    };
    let metadata_json = metadata
        .extensions
        .get("property.x_po_metadata_json")
        .cloned()
        .or_else(|| {
            xliff_properties.as_ref().and_then(|properties| {
                properties
                    .get(PO_METADATA_JSON)
                    .or_else(|| properties.get("x_po_metadata_json"))
                    .cloned()
            })
        });
    if let Some(value) = metadata_json {
        header.fields = parse_metadata_json(&value)?;
    }
    for (extension_key, header_key) in [
        ("po_header_translator_comments", "translator"),
        ("po_header_extracted_comments", "extracted"),
        ("po_header_flags", "flags"),
        ("po_header_previous", "previous"),
    ] {
        let normalized_key = format!("property.{}", extension_key.replace('-', "_"));
        let prefixed_key = format!("property.x_{}", extension_key.replace('-', "_"));
        let value = metadata
            .extensions
            .get(&normalized_key)
            .or_else(|| metadata.extensions.get(&prefixed_key));
        if let Some(value) = value {
            set_header_lines(&mut header, header_key, value);
        }
    }
    if let Some(properties) = xliff_properties {
        for (key, value) in properties {
            match key.as_str() {
                "po_header_translator_comments" | "x_po_header_translator_comments" => {
                    set_header_lines(&mut header, "translator", &value)
                }
                "po_header_extracted_comments" | "x_po_header_extracted_comments" => {
                    set_header_lines(&mut header, "extracted", &value)
                }
                "po_header_flags" | "x_po_header_flags" => {
                    set_header_lines(&mut header, "flags", &value)
                }
                "po_header_previous" | "x_po_header_previous" => {
                    set_header_lines(&mut header, "previous", &value)
                }
                _ => {}
            }
        }
    }
    Ok(header)
}

fn read_xliff_po_header_properties(path: &Path) -> NativeResult<HashMap<String, String>> {
    let file = File::open(path)?;
    let mut reader = XmlReader::from_reader(BufReader::with_capacity(READ_CAPACITY, file));
    reader.config_mut().trim_text(false);
    let mut buffer = Vec::with_capacity(READ_CAPACITY);
    let mut properties = HashMap::new();
    let mut capture: Option<XmlPoCapture> = None;
    loop {
        buffer.clear();
        match reader.read_event_into(&mut buffer)? {
            Event::Start(element) => {
                let local_name = element.local_name();
                if matches!(local_name.as_ref(), b"trans-unit" | b"unit" | b"segment") {
                    break;
                }
                if local_name.as_ref() == b"prop" && capture.is_none() {
                    let key = attribute_value(&element, b"prop-type", reader.decoder())?
                        .or(attribute_value(&element, b"type", reader.decoder())?)
                        .unwrap_or_default();
                    capture = Some(XmlPoCapture {
                        element_name: b"prop".to_vec(),
                        field: XmlPoField::Property(key),
                        value: String::new(),
                    });
                }
            }
            Event::Text(text) => {
                let decoded = text.decode().map_err(|error| {
                    NativeError::Invalid(format!("cannot decode XLIFF header text: {error}"))
                })?;
                let value = quick_xml::escape::unescape(&decoded).map_err(|error| {
                    NativeError::Invalid(format!("cannot unescape XLIFF header text: {error}"))
                })?;
                append_xml_text(&mut capture, &value)?;
            }
            Event::CData(text) => append_xml_text(
                &mut capture,
                &text.decode().map_err(|error| {
                    NativeError::Invalid(format!("cannot decode XLIFF header CDATA: {error}"))
                })?,
            )?,
            Event::GeneralRef(reference) => {
                append_xml_text(&mut capture, &xml_reference_value(&reference)?)?;
            }
            Event::End(element)
                if capture
                    .as_ref()
                    .is_some_and(|value| value.element_name == element.local_name().as_ref()) =>
            {
                let value = capture.take().expect("header property capture exists");
                if let XmlPoField::Property(key) = value.field {
                    properties.insert(normalize_po_xml_key(&key), value.value);
                }
            }
            Event::Eof => break,
            _ => {}
        }
    }
    Ok(properties)
}

fn interchange_po_details(record: &NativeRecord) -> NativeResult<InterchangePoDetails> {
    let Some(fragment) = record.fragment.as_deref() else {
        return Ok(InterchangePoDetails::default());
    };
    let mut reader = XmlReader::from_reader(fragment);
    reader.config_mut().trim_text(false);
    let mut buffer = Vec::with_capacity(fragment.len().min(READ_CAPACITY));
    let mut details = InterchangePoDetails::default();
    let mut capture: Option<XmlPoCapture> = None;
    loop {
        buffer.clear();
        match reader.read_event_into(&mut buffer)? {
            Event::Start(element) => {
                inspect_interchange_po_element(
                    &mut details,
                    &mut capture,
                    &element,
                    reader.decoder(),
                )?;
            }
            Event::Empty(element) => {
                inspect_interchange_po_element(
                    &mut details,
                    &mut capture,
                    &element,
                    reader.decoder(),
                )?;
            }
            Event::Text(text) => {
                let decoded = text.decode().map_err(|error| {
                    NativeError::Invalid(format!("cannot decode interchange text: {error}"))
                })?;
                let value = quick_xml::escape::unescape(&decoded).map_err(|error| {
                    NativeError::Invalid(format!("cannot unescape interchange text: {error}"))
                })?;
                append_xml_text(&mut capture, &value)?;
            }
            Event::CData(text) => {
                let value = text.decode().map_err(|error| {
                    NativeError::Invalid(format!("cannot decode interchange CDATA: {error}"))
                })?;
                append_xml_text(&mut capture, &value)?;
            }
            Event::GeneralRef(reference) => {
                append_xml_text(&mut capture, &xml_reference_value(&reference)?)?;
            }
            Event::End(element)
                if capture
                    .as_ref()
                    .is_some_and(|value| value.element_name == element.local_name().as_ref()) =>
            {
                apply_xml_po_capture(
                    &mut details,
                    capture.take().expect("interchange field capture exists"),
                );
            }
            Event::Eof => break,
            _ => {}
        }
    }
    Ok(details)
}

fn inspect_interchange_po_element(
    details: &mut InterchangePoDetails,
    capture: &mut Option<XmlPoCapture>,
    element: &BytesStart<'_>,
    decoder: quick_xml::encoding::Decoder,
) -> NativeResult<()> {
    let local_name = element.local_name();
    let name = local_name.as_ref();
    if matches!(name, b"trans-unit" | b"unit" | b"segment") {
        details.header = attribute_value(element, b"restype", decoder)?
            .is_some_and(|value| value == "x-gettext-domain-header");
    }
    if capture.is_some() {
        return Ok(());
    }
    let field = match name {
        b"context" => XmlPoField::Context(
            attribute_value(element, b"context-type", decoder)?.unwrap_or_default(),
        ),
        b"prop" => XmlPoField::Property(
            attribute_value(element, b"type", decoder)?
                .or(attribute_value(element, b"prop-type", decoder)?)
                .unwrap_or_default(),
        ),
        b"note" => {
            XmlPoField::Note(attribute_value(element, b"from", decoder)?.unwrap_or_default())
        }
        _ => return Ok(()),
    };
    *capture = Some(XmlPoCapture {
        element_name: name.to_vec(),
        field,
        value: String::new(),
    });
    Ok(())
}

fn append_xml_text(capture: &mut Option<XmlPoCapture>, value: &str) -> NativeResult<()> {
    if let Some(capture) = capture {
        validate_xml_chars(value, "interchange PO field")?;
        capture.value.push_str(value);
    }
    Ok(())
}

fn xml_reference_value(reference: &BytesRef<'_>) -> NativeResult<String> {
    if let Some(character) = reference.resolve_char_ref().map_err(|error| {
        NativeError::Invalid(format!("invalid XML character reference: {error}"))
    })? {
        return Ok(character.to_string());
    }
    let name = reference
        .decode()
        .map_err(|error| NativeError::Invalid(format!("cannot decode XML entity: {error}")))?;
    quick_xml::escape::resolve_predefined_entity(&name)
        .map(str::to_owned)
        .ok_or_else(|| NativeError::Invalid(format!("unresolved XML entity reference: &{name};")))
}

fn apply_xml_po_capture(details: &mut InterchangePoDetails, capture: XmlPoCapture) {
    let XmlPoCapture { field, value, .. } = capture;
    match field {
        XmlPoField::Context(kind) | XmlPoField::Property(kind) => {
            apply_structured_po_value(details, &kind, value)
        }
        XmlPoField::Note(from) if normalize_po_xml_key(&from) == "po_translator" => {
            push_unique(&mut details.translator_comments, value);
        }
        XmlPoField::Note(_) => {
            if !details.translator_comments.contains(&value)
                && !details.extracted_comments.contains(&value)
            {
                details.extracted_comments.push(value);
            }
        }
    }
}

fn apply_structured_po_value(details: &mut InterchangePoDetails, raw_kind: &str, value: String) {
    match normalize_po_xml_key(raw_kind).as_str() {
        "x_po_msgid" => details.msgid = Some(value),
        "x_po_msgctxt" | "x_context" => details.context = Some(value),
        "x_po_msgid_plural" => details.msgid_plural = Some(value),
        "x_po_plural_index" => details.plural_index = value.parse().ok(),
        "x_po_entry_index" => details.entry_index = Some(value),
        "x_po_flags" => {
            details.flags.extend(
                value
                    .split(',')
                    .map(str::trim)
                    .filter(|flag| !flag.is_empty())
                    .map(str::to_owned),
            );
        }
        "x_po_references" => details
            .references
            .extend(parse_references(&value.replace(',', " "))),
        "x_po_previous" => details.previous.extend(value.lines().map(str::to_owned)),
        "x_po_translator_comment" | "x_po_trancomment" => {
            push_unique(&mut details.translator_comments, value);
        }
        "x_po_extracted_comment" | "x_po_autocomment" => {
            push_unique(&mut details.extracted_comments, value);
        }
        "x_po_metadata_json" => details.metadata_json = Some(value),
        "x_lokit_unit_id" => details.lokit_unit_id = Some(value),
        _ => {}
    }
}

fn normalize_po_xml_key(value: &str) -> String {
    value.trim().to_ascii_lowercase().replace(['-', ' '], "_")
}

fn push_unique(values: &mut Vec<String>, value: String) {
    if !values.contains(&value) {
        values.push(value);
    }
}

fn selected_record_target(
    record: &NativeRecord,
    metadata: &Metadata,
) -> NativeResult<Option<String>> {
    if let Some(target) = record.target.as_ref() {
        return Ok(Some(target.clone()));
    }
    if let Some(locale) = metadata.target_locale.as_deref() {
        let canonical = canonical_locale(locale);
        return Ok(record.targets.iter().find_map(|(candidate, target)| {
            (canonical_locale(candidate) == canonical).then(|| target.clone())
        }));
    }
    match record.targets.as_slice() {
        [] => Ok(None),
        [(_, target)] => Ok(Some(target.clone())),
        _ => Err(NativeError::Invalid(format!(
            "translation unit {:?} has multiple targets; select a target locale for PO export",
            record.unit_id
        ))),
    }
}

fn interchange_plural_identity(
    record: &NativeRecord,
    details: &InterchangePoDetails,
) -> (String, u32, bool) {
    if let Some(index) = details.plural_index {
        let identity = details.entry_index.as_ref().map_or_else(
            || record.unit_id.clone(),
            |value| format!("po-entry-{value}"),
        );
        return (identity, index, true);
    }
    let raw_id = record
        .extensions
        .get("unit_id")
        .filter(|value| !value.is_empty())
        .unwrap_or(&record.unit_id);
    if let Some(index) = terminal_plural_index(raw_id) {
        let base = raw_id
            .rsplit_once('[')
            .map_or_else(|| raw_id.clone(), |(value, _)| value.to_owned());
        return (base, index, true);
    }
    (record.unit_id.clone(), 0, details.msgid_plural.is_some())
}

fn po_entry_from_interchange(
    record: &NativeRecord,
    details: &InterchangePoDetails,
    plural: bool,
    plural_index: u32,
) -> PoEntry {
    let id = details
        .msgid
        .clone()
        .unwrap_or_else(|| record.source.clone());
    let id_plural = details.msgid_plural.clone().unwrap_or_else(|| {
        if plural && plural_index > 0 {
            record.source.clone()
        } else {
            String::new()
        }
    });
    let mut entry = PoEntry {
        context: details.context.clone(),
        id,
        id_plural,
        flags: details.flags.clone(),
        extracted_comments: details.extracted_comments.clone(),
        translator_comments: details.translator_comments.clone(),
        references: details.references.clone(),
        previous: details.previous.clone(),
        line: 1,
        ..PoEntry::default()
    };
    if let Some(unit_id) = details.lokit_unit_id.as_deref() {
        entry
            .extracted_comments
            .push(encode_lokit_unit_id_comment(unit_id));
    }
    entry
}

fn merge_interchange_record(
    entry: &mut PoEntry,
    record: &NativeRecord,
    details: &InterchangePoDetails,
    target: &str,
    plural: bool,
    plural_index: u32,
) {
    if plural {
        if plural_index == 0 {
            entry.id = details
                .msgid
                .clone()
                .unwrap_or_else(|| record.source.clone());
        } else if entry.id_plural.is_empty() {
            entry.id_plural = details
                .msgid_plural
                .clone()
                .unwrap_or_else(|| record.source.clone());
        }
        entry
            .plural_translations
            .insert(plural_index, target.to_owned());
    } else {
        entry.translation = target.to_owned();
    }
    if matches!(record.status.as_str(), "draft" | "rejected")
        && !entry.flags.iter().any(|flag| flag == "fuzzy")
    {
        entry.flags.push("fuzzy".to_owned());
    }
    for flag in &details.flags {
        push_unique(&mut entry.flags, flag.clone());
    }
    for value in &details.translator_comments {
        push_unique(&mut entry.translator_comments, value.clone());
    }
    for value in &details.extracted_comments {
        push_unique(&mut entry.extracted_comments, value.clone());
    }
    if let Some(unit_id) = details.lokit_unit_id.as_deref() {
        push_unique(
            &mut entry.extracted_comments,
            encode_lokit_unit_id_comment(unit_id),
        );
    }
    for value in &details.previous {
        push_unique(&mut entry.previous, value.clone());
    }
    for reference in &details.references {
        if !entry.references.contains(reference) {
            entry.references.push(reference.clone());
        }
    }
}

fn merge_header_text(header: &mut PoHeader, value: &str) {
    for line in value.lines() {
        if let Some((key, field_value)) = line.split_once(':') {
            set_header_field(&mut header.fields, key.trim(), field_value.trim());
        }
    }
}

fn merge_details_into_header(
    header: &mut PoHeader,
    details: &InterchangePoDetails,
) -> NativeResult<()> {
    if let Some(value) = details.metadata_json.as_deref() {
        for (key, field_value) in parse_metadata_json(value)? {
            set_header_field(&mut header.fields, &key, &field_value);
        }
    }
    for value in &details.translator_comments {
        push_unique(&mut header.translator_comments, value.clone());
    }
    for value in &details.extracted_comments {
        push_unique(&mut header.extracted_comments, value.clone());
    }
    for value in &details.flags {
        push_unique(&mut header.flags, value.clone());
    }
    for value in &details.previous {
        push_unique(&mut header.previous, value.clone());
    }
    Ok(())
}

fn parse_metadata_json(value: &str) -> NativeResult<Vec<(String, String)>> {
    serde_json::from_str::<BTreeMap<String, String>>(value)
        .map(|fields| fields.into_iter().collect())
        .map_err(|error| NativeError::Invalid(format!("invalid PO metadata JSON: {error}")))
}

fn set_header_lines(header: &mut PoHeader, kind: &str, value: &str) {
    let destination = match kind {
        "translator" => &mut header.translator_comments,
        "extracted" => &mut header.extracted_comments,
        "flags" => &mut header.flags,
        "previous" => &mut header.previous,
        _ => return,
    };
    destination.extend(value.split('\n').map(str::to_owned));
}

fn complete_interchange_header(header: &mut PoHeader, metadata: &Metadata) {
    set_header_field(
        &mut header.fields,
        "Content-Type",
        "text/plain; charset=UTF-8",
    );
    set_header_field(&mut header.fields, "Content-Transfer-Encoding", "8bit");
    if let Some(locale) = metadata.target_locale.as_deref() {
        set_header_field(&mut header.fields, "Language", locale);
    }
}

fn validate_po_metadata_for_xml(parser: &PoParser) -> NativeResult<()> {
    for (key, value) in &parser.header.fields {
        validate_xml_chars(key, "PO metadata key")?;
        validate_xml_chars(value, "PO metadata value")?;
    }
    for value in parser
        .header
        .translator_comments
        .iter()
        .chain(&parser.header.extracted_comments)
        .chain(&parser.header.flags)
        .chain(&parser.header.previous)
    {
        validate_xml_chars(value, "PO header comment")?;
    }
    if let Some(locale) = parser.metadata.source_locale.as_deref() {
        validate_xml_chars(locale, "PO source locale")?;
    }
    if let Some(locale) = parser.metadata.target_locale.as_deref() {
        validate_xml_chars(locale, "PO target locale")?;
    }
    Ok(())
}

fn validate_po_entry_for_xml(entry: &PoEntry) -> NativeResult<()> {
    validate_xml_chars(&entry.id, "PO msgid")?;
    validate_xml_chars(&entry.id_plural, "PO msgid_plural")?;
    validate_xml_chars(&entry.translation, "PO msgstr")?;
    if let Some(context) = entry.context.as_deref() {
        validate_xml_chars(context, "PO msgctxt")?;
    }
    for value in entry
        .plural_translations
        .values()
        .chain(entry.flags.iter())
        .chain(entry.extracted_comments.iter())
        .chain(entry.translator_comments.iter())
        .chain(entry.previous.iter())
    {
        validate_xml_chars(value, "PO entry field")?;
    }
    for (path, line) in &entry.references {
        validate_xml_chars(path, "PO reference path")?;
        validate_xml_chars(line, "PO reference line")?;
    }
    Ok(())
}

fn validate_xml_chars(value: &str, field: &str) -> NativeResult<()> {
    if is_xml_1_0_chars(value) {
        return Ok(());
    }
    let character = value
        .chars()
        .find(|character| !is_xml_1_0_chars(&character.to_string()))
        .expect("invalid XML text contains an invalid character");
    Err(NativeError::Invalid(format!(
        "{field} contains U+{:04X}, which is not permitted in XML 1.0",
        u32::from(character)
    )))
}

fn write_po_tmx_start<W: Write>(
    writer: &mut Writer<W>,
    po_header: &PoHeader,
    metadata: &Metadata,
) -> NativeResult<()> {
    let mut root = BytesStart::new("tmx");
    root.push_attribute(("version", "1.4"));
    writer.write_event(Event::Start(root))?;
    let source_locale = metadata.source_locale.as_deref().unwrap_or("und");
    let mut header = BytesStart::new("header");
    header.push_attribute(("creationtool", "lokit"));
    header.push_attribute(("creationtoolversion", env!("CARGO_PKG_VERSION")));
    header.push_attribute(("segtype", "sentence"));
    header.push_attribute(("o-tmf", "lokit-gettext"));
    header.push_attribute(("adminlang", source_locale));
    header.push_attribute(("srclang", source_locale));
    header.push_attribute(("datatype", "text"));
    writer.write_event(Event::Start(header))?;
    if !po_header.fields.is_empty() {
        write_tmx_prop(writer, "x-po-metadata-json", &po_header.metadata_json()?)?;
    }
    write_po_header_fields_tmx(writer, po_header)?;
    writer.write_event(Event::End(BytesEnd::new("header")))?;
    writer.write_event(Event::Start(BytesStart::new("body")))?;
    Ok(())
}

fn write_po_xliff_start<W: Write>(
    writer: &mut Writer<W>,
    source_path: &Path,
    po_header: &PoHeader,
    metadata: &Metadata,
) -> NativeResult<()> {
    let mut root = BytesStart::new("xliff");
    root.push_attribute(("xmlns", "urn:oasis:names:tc:xliff:document:1.2"));
    root.push_attribute(("version", "1.2"));
    writer.write_event(Event::Start(root))?;
    let mut file = BytesStart::new("file");
    file.push_attribute(("original", source_path.to_string_lossy().as_ref()));
    file.push_attribute(("datatype", "gettext"));
    file.push_attribute((
        "source-language",
        metadata.source_locale.as_deref().unwrap_or("und"),
    ));
    if let Some(locale) = metadata.target_locale.as_deref() {
        file.push_attribute(("target-language", locale));
    }
    writer.write_event(Event::Start(file))?;
    writer.write_event(Event::Start(BytesStart::new("header")))?;
    if !po_header.fields.is_empty()
        || !po_header.translator_comments.is_empty()
        || !po_header.extracted_comments.is_empty()
        || !po_header.flags.is_empty()
        || !po_header.previous.is_empty()
    {
        let mut group = BytesStart::new("prop-group");
        group.push_attribute(("name", "lokit-gettext-header"));
        writer.write_event(Event::Start(group))?;
        if !po_header.fields.is_empty() {
            write_xliff_prop(writer, "x-po-metadata-json", &po_header.metadata_json()?)?;
        }
        write_po_header_fields_xliff(writer, po_header)?;
        writer.write_event(Event::End(BytesEnd::new("prop-group")))?;
    }
    writer.write_event(Event::End(BytesEnd::new("header")))?;
    writer.write_event(Event::Start(BytesStart::new("body")))?;
    Ok(())
}

fn write_po_entry_tmx<W: Write>(
    writer: &mut Writer<W>,
    entry: &PoEntry,
    index: usize,
    metadata: &Metadata,
    mode: PoImportMode,
) -> NativeResult<usize> {
    let indexes = po_form_indexes(entry);
    for form_index in &indexes {
        let mut unit = BytesStart::new("tu");
        let id = if entry.id_plural.is_empty() {
            format!("po-{index}")
        } else {
            format!("po-{index}[{form_index}]")
        };
        unit.push_attribute(("tuid", id.as_str()));
        writer.write_event(Event::Start(unit))?;
        write_po_properties_tmx(writer, entry, *form_index, index)?;
        let (source, target) = po_form_text(entry, *form_index, mode);
        write_tmx_variant(
            writer,
            metadata.source_locale.as_deref().unwrap_or("und"),
            &source,
        )?;
        if let (Some(locale), Some(target)) = (metadata.target_locale.as_deref(), target) {
            write_tmx_variant(writer, locale, &target)?;
        }
        if *form_index == 0 {
            for comment in &entry.translator_comments {
                write_text_element(writer, "note", comment)?;
            }
            for comment in entry
                .extracted_comments
                .iter()
                .filter(|comment| decode_lokit_unit_id_comment(comment).is_none())
            {
                write_text_element(writer, "note", comment)?;
            }
        }
        writer.write_event(Event::End(BytesEnd::new("tu")))?;
    }
    Ok(indexes.len())
}

fn write_po_entry_xliff<W: Write>(
    writer: &mut Writer<W>,
    entry: &PoEntry,
    index: usize,
    _metadata: &Metadata,
    mode: PoImportMode,
) -> NativeResult<usize> {
    let indexes = po_form_indexes(entry);
    if !entry.id_plural.is_empty() {
        let mut group = BytesStart::new("group");
        group.push_attribute(("id", format!("po-{index}").as_str()));
        group.push_attribute(("restype", "x-gettext-plurals"));
        writer.write_event(Event::Start(group))?;
    }
    for form_index in &indexes {
        let id = if entry.id_plural.is_empty() {
            format!("po-{index}")
        } else {
            format!("po-{index}[{form_index}]")
        };
        let mut unit = BytesStart::new("trans-unit");
        unit.push_attribute(("id", id.as_str()));
        writer.write_event(Event::Start(unit))?;
        let (source, target) = po_form_text(entry, *form_index, mode);
        write_text_element(writer, "source", &source)?;
        if let Some(target) = target {
            let mut target_element = BytesStart::new("target");
            target_element.push_attribute((
                "state",
                if entry.flags.iter().any(|flag| flag == "fuzzy") {
                    "needs-translation"
                } else {
                    "translated"
                },
            ));
            writer.write_event(Event::Start(target_element))?;
            writer.write_event(Event::Text(BytesText::new(&target)))?;
            writer.write_event(Event::End(BytesEnd::new("target")))?;
        }
        write_po_contexts_xliff(writer, entry, *form_index, index)?;
        if *form_index == 0 {
            for comment in &entry.translator_comments {
                write_xliff_note(writer, "po-translator", comment)?;
            }
            for comment in entry
                .extracted_comments
                .iter()
                .filter(|comment| decode_lokit_unit_id_comment(comment).is_none())
            {
                write_xliff_note(writer, "developer", comment)?;
            }
        }
        writer.write_event(Event::End(BytesEnd::new("trans-unit")))?;
    }
    if !entry.id_plural.is_empty() {
        writer.write_event(Event::End(BytesEnd::new("group")))?;
    }
    Ok(indexes.len())
}

fn po_form_indexes(entry: &PoEntry) -> Vec<u32> {
    if entry.id_plural.is_empty() {
        return vec![0];
    }
    let mut indexes = entry
        .plural_translations
        .keys()
        .copied()
        .collect::<Vec<_>>();
    if !indexes.contains(&0) {
        indexes.push(0);
    }
    indexes.sort_unstable();
    indexes.dedup();
    indexes
}

fn po_form_text(entry: &PoEntry, index: u32, mode: PoImportMode) -> (String, Option<String>) {
    let original_source = if index == 0 {
        entry.id.clone()
    } else {
        entry.id_plural.clone()
    };
    let translation = if entry.id_plural.is_empty() {
        entry.translation.clone()
    } else {
        entry
            .plural_translations
            .get(&index)
            .cloned()
            .unwrap_or_default()
    };
    match mode {
        PoImportMode::TargetAsSource => (
            if translation.is_empty() {
                original_source
            } else {
                translation
            },
            None,
        ),
        PoImportMode::Gettext => (
            original_source,
            (!translation.is_empty()).then_some(translation),
        ),
        PoImportMode::Auto | PoImportMode::Source => (original_source, None),
    }
}

fn write_po_properties_tmx<W: Write>(
    writer: &mut Writer<W>,
    entry: &PoEntry,
    form_index: u32,
    entry_index: usize,
) -> NativeResult<()> {
    if let Some(unit_id) = entry
        .extracted_comments
        .iter()
        .find_map(|comment| decode_lokit_unit_id_comment(comment))
    {
        write_tmx_prop(writer, "x-lokit-unit-id", &unit_id)?;
    }
    write_tmx_prop(writer, "x-po-msgid", &entry.id)?;
    if let Some(context) = entry.context.as_deref() {
        write_tmx_prop(writer, "x-po-msgctxt", context)?;
    }
    if !entry.id_plural.is_empty() {
        write_tmx_prop(writer, "x-po-msgid-plural", &entry.id_plural)?;
        write_tmx_prop(writer, "x-po-plural-index", &form_index.to_string())?;
        write_tmx_prop(writer, "x-po-entry-index", &entry_index.to_string())?;
    }
    if !entry.flags.is_empty() {
        write_tmx_prop(writer, "x-po-flags", &entry.flags.join(", "))?;
    }
    if !entry.references.is_empty() {
        write_tmx_prop(
            writer,
            "x-po-references",
            &format_references(&entry.references),
        )?;
    }
    for comment in &entry.translator_comments {
        write_tmx_prop(writer, "x-po-translator-comment", comment)?;
    }
    for comment in entry
        .extracted_comments
        .iter()
        .filter(|comment| decode_lokit_unit_id_comment(comment).is_none())
    {
        write_tmx_prop(writer, "x-po-extracted-comment", comment)?;
    }
    for previous in &entry.previous {
        write_tmx_prop(writer, "x-po-previous", previous)?;
    }
    Ok(())
}

fn write_tmx_prop<W: Write>(writer: &mut Writer<W>, kind: &str, value: &str) -> NativeResult<()> {
    let mut property = BytesStart::new("prop");
    property.push_attribute(("type", kind));
    writer.write_event(Event::Start(property))?;
    writer.write_event(Event::Text(BytesText::new(value)))?;
    writer.write_event(Event::End(BytesEnd::new("prop")))?;
    Ok(())
}

fn write_po_header_fields_tmx<W: Write>(
    writer: &mut Writer<W>,
    header: &PoHeader,
) -> NativeResult<()> {
    for (kind, values) in [
        (
            "x-po-header-translator-comments",
            &header.translator_comments,
        ),
        ("x-po-header-extracted-comments", &header.extracted_comments),
        ("x-po-header-flags", &header.flags),
        ("x-po-header-previous", &header.previous),
    ] {
        if !values.is_empty() {
            write_tmx_prop(writer, kind, &values.join("\n"))?;
        }
    }
    Ok(())
}

fn write_tmx_variant<W: Write>(
    writer: &mut Writer<W>,
    locale: &str,
    text: &str,
) -> NativeResult<()> {
    let mut variant = BytesStart::new("tuv");
    variant.push_attribute(("xml:lang", locale));
    writer.write_event(Event::Start(variant))?;
    write_text_element(writer, "seg", text)?;
    writer.write_event(Event::End(BytesEnd::new("tuv")))?;
    Ok(())
}

fn write_po_contexts_xliff<W: Write>(
    writer: &mut Writer<W>,
    entry: &PoEntry,
    form_index: u32,
    entry_index: usize,
) -> NativeResult<()> {
    writer.write_event(Event::Start({
        let mut group = BytesStart::new("context-group");
        group.push_attribute(("name", "lokit-gettext"));
        group.push_attribute(("purpose", "information"));
        group
    }))?;
    write_xliff_context(writer, "x-po-msgid", &entry.id)?;
    if let Some(unit_id) = entry
        .extracted_comments
        .iter()
        .find_map(|comment| decode_lokit_unit_id_comment(comment))
    {
        write_xliff_context(writer, "x-lokit-unit-id", &unit_id)?;
    }
    if let Some(context) = entry.context.as_deref() {
        write_xliff_context(writer, "x-po-msgctxt", context)?;
    }
    if !entry.id_plural.is_empty() {
        write_xliff_context(writer, "x-po-msgid-plural", &entry.id_plural)?;
        write_xliff_context(writer, "x-po-plural-index", &form_index.to_string())?;
        write_xliff_context(writer, "x-po-entry-index", &entry_index.to_string())?;
    }
    if !entry.flags.is_empty() {
        write_xliff_context(writer, "x-po-flags", &entry.flags.join(", "))?;
    }
    if !entry.references.is_empty() {
        write_xliff_context(
            writer,
            "x-po-references",
            &format_references(&entry.references),
        )?;
    }
    for previous in &entry.previous {
        write_xliff_context(writer, "x-po-previous", previous)?;
    }
    writer.write_event(Event::End(BytesEnd::new("context-group")))?;
    Ok(())
}

fn write_xliff_context<W: Write>(
    writer: &mut Writer<W>,
    kind: &str,
    value: &str,
) -> NativeResult<()> {
    let mut context = BytesStart::new("context");
    context.push_attribute(("context-type", kind));
    writer.write_event(Event::Start(context))?;
    writer.write_event(Event::Text(BytesText::new(value)))?;
    writer.write_event(Event::End(BytesEnd::new("context")))?;
    Ok(())
}

fn write_xliff_note<W: Write>(writer: &mut Writer<W>, from: &str, value: &str) -> NativeResult<()> {
    let mut note = BytesStart::new("note");
    note.push_attribute(("from", from));
    writer.write_event(Event::Start(note))?;
    writer.write_event(Event::Text(BytesText::new(value)))?;
    writer.write_event(Event::End(BytesEnd::new("note")))?;
    Ok(())
}

fn write_xliff_prop<W: Write>(writer: &mut Writer<W>, kind: &str, value: &str) -> NativeResult<()> {
    let mut property = BytesStart::new("prop");
    property.push_attribute(("prop-type", kind));
    writer.write_event(Event::Start(property))?;
    writer.write_event(Event::Text(BytesText::new(value)))?;
    writer.write_event(Event::End(BytesEnd::new("prop")))?;
    Ok(())
}

fn write_po_header_fields_xliff<W: Write>(
    writer: &mut Writer<W>,
    header: &PoHeader,
) -> NativeResult<()> {
    for (kind, values) in [
        (
            "x-po-header-translator-comments",
            &header.translator_comments,
        ),
        ("x-po-header-extracted-comments", &header.extracted_comments),
        ("x-po-header-flags", &header.flags),
        ("x-po-header-previous", &header.previous),
    ] {
        if !values.is_empty() {
            write_xliff_prop(writer, kind, &values.join("\n"))?;
        }
    }
    Ok(())
}

fn write_text_element<W: Write>(
    writer: &mut Writer<W>,
    name: &str,
    value: &str,
) -> NativeResult<()> {
    writer.write_event(Event::Start(BytesStart::new(name)))?;
    if !value.is_empty() {
        writer.write_event(Event::Text(BytesText::new(value)))?;
    }
    writer.write_event(Event::End(BytesEnd::new(name)))?;
    Ok(())
}

fn format_references(references: &[(String, String)]) -> String {
    references
        .iter()
        .map(|(path, line)| {
            if line.is_empty() {
                path.clone()
            } else {
                format!("{path}:{line}")
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

#[pyfunction]
#[pyo3(signature = (document, target_path, mode="auto"))]
fn export_base_po(
    py: Python<'_>,
    document: &Bound<'_, PyAny>,
    target_path: &str,
    mode: &str,
) -> PyResult<Option<usize>> {
    let mode = PoImportMode::parse(mode).map_err(native_to_py_error)?;
    let source_locale: String = document.getattr("source_locale")?.extract()?;
    let target_locale: Option<String> = document.getattr("target_locale")?.extract()?;
    let target_locales: Vec<String> = document.getattr("target_locales")?.extract()?;
    if target_locale.is_none() && !target_locales.is_empty() {
        return Ok(None);
    }
    let mut extensions = string_map_from_python(&document.getattr("extensions")?)?;
    let data = document.getattr("data")?;
    let data = data
        .cast::<PyDict>()
        .map_err(|_| PyValueError::new_err("BaseStructure.data must be a dict"))?;
    let mut units = Vec::with_capacity(data.len());
    for (unit_id, value) in data.iter() {
        let unit = data_from_python(&value)?;
        if is_gettext_header_data(&unit) {
            merge_gettext_header_data(&mut extensions, &unit).map_err(native_to_py_error)?;
        } else {
            units.push((unit_id.extract()?, unit));
        }
    }
    let path = target_path.to_owned();
    py.detach(move || {
        write_base_po(
            Path::new(&path),
            &source_locale,
            target_locale.as_deref(),
            &extensions,
            units,
            mode,
        )
    })
    .map(Some)
    .map_err(native_to_py_error)
}

#[pyfunction]
#[pyo3(signature = (document, target_path, output_format))]
fn export_base_po_interchange(
    py: Python<'_>,
    document: &Bound<'_, PyAny>,
    target_path: &str,
    output_format: &str,
) -> PyResult<Option<usize>> {
    let output_format = InterchangeFormat::parse(output_format).map_err(native_to_py_error)?;
    let source_locale: String = document.getattr("source_locale")?.extract()?;
    let target_locale: Option<String> = document.getattr("target_locale")?.extract()?;
    let target_locales: Vec<String> = document.getattr("target_locales")?.extract()?;
    if target_locale.is_none() && !target_locales.is_empty() {
        return Ok(None);
    }
    let extensions = string_map_from_python(&document.getattr("extensions")?)?;
    if extensions.get("input_format").map(String::as_str) != Some("po") {
        return Ok(None);
    }
    let mode = extensions
        .get("po_import_mode")
        .map_or(Ok(PoImportMode::Gettext), |value| {
            PoImportMode::parse(value)
        })
        .map_err(native_to_py_error)?;
    let data = document.getattr("data")?;
    let data = data
        .cast::<PyDict>()
        .map_err(|_| PyValueError::new_err("BaseStructure.data must be a dict"))?;
    let mut units = Vec::with_capacity(data.len());
    for (unit_id, value) in data.iter() {
        units.push((unit_id.extract()?, data_from_python(&value)?));
    }
    let path = target_path.to_owned();
    py.detach(move || {
        let header = header_from_extensions(&extensions, target_locale.as_deref())?;
        let entries =
            po_entries_from_units(units, target_locale.as_deref(), mode, po_nplurals(&header))?;
        let mut metadata = Metadata {
            source_locale: (!source_locale.is_empty()).then_some(source_locale.clone()),
            source_language: (!source_locale.is_empty()).then(|| base_language(&source_locale)),
            ..Metadata::default()
        };
        if let Some(locale) = target_locale {
            metadata.target_locale = Some(locale.clone());
            metadata.target_locales.push(locale.clone());
            metadata.target_language = Some(base_language(&locale));
            metadata.target_languages.push(base_language(&locale));
        }
        let source_path = extensions
            .get("source_path")
            .or_else(|| extensions.get("resource_key"))
            .map(String::as_str)
            .unwrap_or("lokit");
        write_base_po_interchange(
            Path::new(&path),
            Path::new(source_path),
            output_format,
            &header,
            &metadata,
            &entries,
            mode,
        )
    })
    .map(Some)
    .map_err(native_to_py_error)
}

fn write_base_po_interchange(
    path: &Path,
    source_path: &Path,
    output_format: InterchangeFormat,
    header: &PoHeader,
    metadata: &Metadata,
    entries: &[PoEntry],
    mode: PoImportMode,
) -> NativeResult<usize> {
    for (key, value) in &header.fields {
        validate_xml_chars(key, "PO metadata key")?;
        validate_xml_chars(value, "PO metadata value")?;
    }
    for entry in entries {
        validate_po_entry_for_xml(entry)?;
    }
    let file = File::create(path)?;
    let stream = BufWriter::with_capacity(READ_CAPACITY, file);
    let mut writer = Writer::new(stream);
    writer.write_event(Event::Decl(BytesDecl::new("1.0", Some("UTF-8"), None)))?;
    match output_format {
        InterchangeFormat::Tmx => write_po_tmx_start(&mut writer, header, metadata)?,
        InterchangeFormat::Xliff => {
            write_po_xliff_start(&mut writer, source_path, header, metadata)?
        }
    }
    let mut units = 0;
    for (index, entry) in entries.iter().enumerate() {
        match output_format {
            InterchangeFormat::Tmx => {
                units += write_po_entry_tmx(&mut writer, entry, index, metadata, mode)?
            }
            InterchangeFormat::Xliff => {
                units += write_po_entry_xliff(&mut writer, entry, index, metadata, mode)?
            }
        }
    }
    match output_format {
        InterchangeFormat::Tmx => {
            writer.write_event(Event::End(BytesEnd::new("body")))?;
            writer.write_event(Event::End(BytesEnd::new("tmx")))?;
        }
        InterchangeFormat::Xliff => {
            writer.write_event(Event::End(BytesEnd::new("body")))?;
            writer.write_event(Event::End(BytesEnd::new("file")))?;
            writer.write_event(Event::End(BytesEnd::new("xliff")))?;
        }
    }
    writer.get_mut().write_all(b"\n")?;
    writer.get_mut().flush()?;
    Ok(units)
}

fn string_map_from_python(value: &Bound<'_, PyAny>) -> PyResult<HashMap<String, String>> {
    let values = value
        .cast::<PyDict>()
        .map_err(|_| PyValueError::new_err("extensions must be a dict"))?;
    values
        .iter()
        .map(|(key, value)| Ok((key.extract()?, value.extract()?)))
        .collect()
}

fn is_gettext_header_data(data: &Data) -> bool {
    data.extensions
        .iter()
        .any(|(key, value)| key == "xliff_restype" && value == "x-gettext-domain-header")
}

fn merge_gettext_header_data(
    document_extensions: &mut HashMap<String, String>,
    data: &Data,
) -> NativeResult<()> {
    let value = data.target.as_deref().unwrap_or(&data.source);
    let fields = value
        .lines()
        .filter_map(|line| {
            line.split_once(':')
                .map(|(key, value)| (key.trim().to_owned(), value.trim().to_owned()))
        })
        .collect::<BTreeMap<_, _>>();
    if !fields.is_empty() {
        document_extensions.insert(
            PO_METADATA_JSON.to_owned(),
            serde_json::to_string(&fields).map_err(|error| {
                NativeError::Invalid(format!("cannot encode PO metadata: {error}"))
            })?,
        );
    }
    for comment in &data.comments {
        let kind = comment
            .extensions
            .iter()
            .find_map(|(key, value)| (key == PO_COMMENT_KIND).then_some(value.as_str()));
        let key = if kind == Some("translator") {
            "po_header_translator_comments"
        } else {
            "po_header_extracted_comments"
        };
        document_extensions
            .entry(key.to_owned())
            .and_modify(|value| {
                value.push('\n');
                value.push_str(&comment.context);
            })
            .or_insert_with(|| comment.context.clone());
    }
    Ok(())
}

fn write_base_po(
    path: &Path,
    _source_locale: &str,
    target_locale: Option<&str>,
    document_extensions: &HashMap<String, String>,
    units: Vec<(String, Data)>,
    mode: PoImportMode,
) -> NativeResult<usize> {
    let header = header_from_extensions(document_extensions, target_locale)?;
    let entries = po_entries_from_units(units, target_locale, mode, po_nplurals(&header))?;
    let file = File::create(path)?;
    let mut writer = BufWriter::with_capacity(READ_CAPACITY, file);
    write_po_header(&mut writer, &header)?;
    for entry in &entries {
        write_po_entry(&mut writer, entry)?;
    }
    writer.flush()?;
    Ok(entries.len())
}

fn po_entries_from_units(
    units: Vec<(String, Data)>,
    target_locale: Option<&str>,
    mode: PoImportMode,
    nplurals: Option<u32>,
) -> NativeResult<Vec<PoEntry>> {
    let mut entries: Vec<PoEntry> = Vec::new();
    let mut entry_indexes = BoundedIdRegistry::default();
    for (unit_id, data) in units {
        let (base_id, plural_index) = data_plural_identity(&unit_id, &data);
        if data.plural.is_some() && nplurals.is_some_and(|count| plural_index >= count) {
            continue;
        }
        let new_index = u64::try_from(entries.len())
            .map_err(|_| NativeError::Invalid("PO entry index exceeds u64".to_owned()))?;
        let index = if let Some(index) = entry_indexes.get_or_insert(&base_id, new_index)? {
            usize::try_from(index)
                .map_err(|_| NativeError::Invalid("PO entry index exceeds usize".to_owned()))?
        } else {
            let index = entries.len();
            entries.push(po_entry_from_data(&unit_id, &data, mode));
            index
        };
        merge_data_into_entry(&mut entries[index], &data, plural_index, target_locale);
    }
    Ok(entries)
}

fn po_nplurals(header: &PoHeader) -> Option<u32> {
    header.get("Plural-Forms").and_then(|value| {
        value.split(';').find_map(|field| {
            field
                .trim()
                .strip_prefix("nplurals")?
                .trim_start()
                .strip_prefix('=')?
                .trim()
                .parse()
                .ok()
        })
    })
}

fn data_plural_identity(unit_id: &str, data: &Data) -> (String, u32) {
    let entry_index = data
        .extensions
        .iter()
        .find_map(|(key, value)| (key == PO_ENTRY_INDEX).then_some(value));
    let index = data
        .plural
        .as_ref()
        .and_then(|plural| {
            plural
                .extensions
                .iter()
                .find_map(|(key, value)| (key == "gettext_index").then_some(value))
        })
        .or_else(|| {
            data.extensions
                .iter()
                .find_map(|(key, value)| (key == "gettext_index").then_some(value))
        })
        .and_then(|value| value.parse().ok())
        .or_else(|| terminal_plural_index(unit_id))
        .unwrap_or(0);
    let base = entry_index.map_or_else(
        || {
            terminal_plural_index(unit_id)
                .and_then(|_| unit_id.rsplit_once('[').map(|(base, _)| base.to_owned()))
                .unwrap_or_else(|| unit_id.to_owned())
        },
        |value| format!("po-entry-{value}"),
    );
    (base, index)
}

fn terminal_plural_index(value: &str) -> Option<u32> {
    let (base, suffix) = value.rsplit_once('[')?;
    if base.is_empty() || !suffix.ends_with(']') {
        return None;
    }
    suffix[..suffix.len() - 1].parse().ok()
}

fn po_entry_from_data(unit_id: &str, data: &Data, mode: PoImportMode) -> PoEntry {
    let extensions = data.extensions.iter().cloned().collect::<HashMap<_, _>>();
    let legacy = unit_id.split_once('\u{4}');
    let id = extensions
        .get(PO_MSGID)
        .cloned()
        .or_else(|| legacy.map(|(_, id)| id.to_owned()))
        .unwrap_or_else(|| match mode {
            PoImportMode::TargetAsSource => unit_id.to_owned(),
            _ => data.source.clone(),
        });
    let context = extensions
        .get(PO_MSGCTXT)
        .cloned()
        .or_else(|| legacy.map(|(context, _)| context.to_owned()))
        .or_else(|| {
            data.comments
                .iter()
                .find_map(|comment| comment.context_key.clone())
        });
    let mut entry = PoEntry {
        context,
        id,
        id_plural: extensions
            .get(PO_MSGID_PLURAL)
            .cloned()
            .or_else(|| data.plural.as_ref().map(|plural| plural.variant.clone()))
            .unwrap_or_default(),
        flags: extension_list(&extensions, "flags", ','),
        references: extensions
            .get("references")
            .map_or_else(Vec::new, |value| parse_references(&value.replace(',', " "))),
        previous: extensions
            .get(PO_PREVIOUS)
            .map_or_else(Vec::new, |value| value.lines().map(str::to_owned).collect()),
        line: 1,
        ..PoEntry::default()
    };
    for comment in &data.comments {
        let comment_extensions = comment
            .extensions
            .iter()
            .cloned()
            .collect::<HashMap<_, _>>();
        if comment_extensions
            .get(PO_COMMENT_KIND)
            .is_some_and(|kind| kind == "translator")
        {
            entry.translator_comments.push(comment.context.clone());
        } else {
            entry.extracted_comments.push(comment.context.clone());
        }
    }
    let derived_unit_id = if data.plural.is_some() {
        terminal_plural_index(unit_id)
            .and_then(|_| unit_id.rsplit_once('[').map(|(base, _)| base))
            .unwrap_or(unit_id)
    } else {
        unit_id
    };
    let preserved_unit_id = extensions
        .get(LOKIT_UNIT_ID)
        .map_or(derived_unit_id, String::as_str);
    let naturally_roundtrips = entry.context.is_none()
        && !entry.id.is_empty()
        && is_xml_1_0_chars(&entry.id)
        && preserved_unit_id == entry.id;
    let preserve_unit_id =
        extensions.contains_key(LOKIT_UNIT_ID) || !extensions.contains_key(PO_MSGID);
    if preserve_unit_id && !naturally_roundtrips {
        entry
            .extracted_comments
            .push(encode_lokit_unit_id_comment(preserved_unit_id));
    }
    entry
}

fn extension_list(extensions: &HashMap<String, String>, key: &str, delimiter: char) -> Vec<String> {
    extensions.get(key).map_or_else(Vec::new, |value| {
        value
            .split(delimiter)
            .map(str::trim)
            .filter(|value| !value.is_empty() && *value != "fuzzy")
            .map(str::to_owned)
            .collect()
    })
}

fn merge_data_into_entry(
    entry: &mut PoEntry,
    data: &Data,
    plural_index: u32,
    target_locale: Option<&str>,
) {
    let selected_target = data
        .target
        .clone()
        .or_else(|| {
            target_locale.and_then(|locale| {
                data.targets.iter().find_map(|(key, target)| {
                    (key == locale).then(|| target.text.clone()).flatten()
                })
            })
        })
        .or_else(|| {
            (data.targets.len() == 1)
                .then(|| data.targets[0].1.text.clone())
                .flatten()
        })
        .unwrap_or_default();
    if entry.id_plural.is_empty() && data.plural.is_none() {
        entry.translation = selected_target;
    } else {
        if entry.id_plural.is_empty() {
            entry.id_plural = data
                .plural
                .as_ref()
                .map_or_else(|| data.source.clone(), |plural| plural.variant.clone());
        }
        entry
            .plural_translations
            .insert(plural_index, selected_target);
    }
    for comment in &data.comments {
        let kind = comment
            .extensions
            .iter()
            .find_map(|(key, value)| (key == PO_COMMENT_KIND).then_some(value.as_str()));
        let destination = if kind == Some("translator") {
            &mut entry.translator_comments
        } else {
            &mut entry.extracted_comments
        };
        push_unique(destination, comment.context.clone());
    }
    if data.status == TranslationStatus::Draft && !entry.flags.iter().any(|flag| flag == "fuzzy") {
        entry.flags.insert(0, "fuzzy".to_owned());
    }
}

fn header_from_extensions(
    extensions: &HashMap<String, String>,
    target_locale: Option<&str>,
) -> NativeResult<PoHeader> {
    let mut header = PoHeader::default();
    if let Some(raw) = extension_alias(
        extensions,
        &[PO_METADATA_JSON, "property.x_po_metadata_json"],
    ) {
        let values: BTreeMap<String, String> = serde_json::from_str(raw).map_err(|error| {
            NativeError::Invalid(format!("invalid po_metadata_json extension: {error}"))
        })?;
        header.fields.extend(values);
    }
    set_header_field(
        &mut header.fields,
        "Content-Type",
        "text/plain; charset=UTF-8",
    );
    set_header_field(&mut header.fields, "Content-Transfer-Encoding", "8bit");
    if let Some(locale) = target_locale {
        set_header_field(&mut header.fields, "Language", locale);
    }
    header.translator_comments = extension_alias_lines(
        extensions,
        &[
            "po_header_translator_comments",
            "property.x_po_header_translator_comments",
        ],
    );
    header.extracted_comments = extension_alias_lines(
        extensions,
        &[
            "po_header_extracted_comments",
            "property.x_po_header_extracted_comments",
        ],
    );
    header.flags = extension_alias_lines(
        extensions,
        &["po_header_flags", "property.x_po_header_flags"],
    );
    header.previous = extension_alias_lines(
        extensions,
        &["po_header_previous", "property.x_po_header_previous"],
    );
    Ok(header)
}

fn extension_alias<'a>(extensions: &'a HashMap<String, String>, keys: &[&str]) -> Option<&'a str> {
    keys.iter()
        .find_map(|key| extensions.get(*key).map(String::as_str))
}

fn extension_alias_lines(extensions: &HashMap<String, String>, keys: &[&str]) -> Vec<String> {
    extension_alias(extensions, keys).map_or_else(Vec::new, |value| {
        value.split('\n').map(str::to_owned).collect()
    })
}

fn set_header_field(fields: &mut Vec<(String, String)>, key: &str, value: &str) {
    if let Some((_, existing)) = fields.iter_mut().find(|(candidate, _)| candidate == key) {
        *existing = value.to_owned();
    } else {
        fields.push((key.to_owned(), value.to_owned()));
    }
}

fn write_po_header<W: Write>(writer: &mut W, header: &PoHeader) -> NativeResult<()> {
    write_po_comments(writer, "#", &header.translator_comments)?;
    write_po_comments(writer, "#.", &header.extracted_comments)?;
    if !header.flags.is_empty() {
        writeln!(writer, "#, {}", header.flags.join(", "))?;
    }
    write_po_comments(writer, "#|", &header.previous)?;
    writeln!(writer, "msgid \"\"")?;
    writeln!(writer, "msgstr \"\"")?;
    for (key, value) in &header.fields {
        writeln!(writer, "\"{}: {}\\n\"", po_escape(key), po_escape(value))?;
    }
    writeln!(writer)?;
    Ok(())
}

fn write_po_entry<W: Write>(writer: &mut W, entry: &PoEntry) -> NativeResult<()> {
    write_po_comments(writer, "#", &entry.translator_comments)?;
    write_po_comments(writer, "#.", &entry.extracted_comments)?;
    if !entry.references.is_empty() {
        writeln!(writer, "#: {}", format_references(&entry.references))?;
    }
    if !entry.flags.is_empty() {
        writeln!(writer, "#, {}", entry.flags.join(", "))?;
    }
    write_po_comments(writer, "#|", &entry.previous)?;
    if let Some(context) = entry.context.as_deref() {
        writeln!(writer, "msgctxt \"{}\"", po_escape(context))?;
    }
    writeln!(writer, "msgid \"{}\"", po_escape(&entry.id))?;
    if entry.id_plural.is_empty() {
        writeln!(writer, "msgstr \"{}\"", po_escape(&entry.translation))?;
    } else {
        writeln!(writer, "msgid_plural \"{}\"", po_escape(&entry.id_plural))?;
        for (index, translation) in &entry.plural_translations {
            writeln!(writer, "msgstr[{index}] \"{}\"", po_escape(translation))?;
        }
    }
    writeln!(writer)?;
    Ok(())
}

fn write_po_comments<W: Write>(
    writer: &mut W,
    prefix: &str,
    values: &[String],
) -> NativeResult<()> {
    for value in values {
        for line in value.split('\n') {
            writeln!(writer, "{prefix} {line}")?;
        }
    }
    Ok(())
}

fn po_escape(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '\\' => escaped.push_str("\\\\"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\u{7}' => escaped.push_str("\\a"),
            '\u{8}' => escaped.push_str("\\b"),
            '\u{B}' => escaped.push_str("\\v"),
            '\u{C}' => escaped.push_str("\\f"),
            other => escaped.push(other),
        }
    }
    escaped
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PoReader>()?;
    module.add_function(wrap_pyfunction!(materialize_po, module)?)?;
    module.add_function(wrap_pyfunction!(export_base_po, module)?)?;
    module.add_function(wrap_pyfunction!(export_base_po_interchange, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use lokit_format::{Data, PluralCategory, TranslationStatus};

    use super::{
        convert_interchange_to_po, convert_po_to_interchange, entry_comments,
        po_entries_from_units, po_unit_id, PoImportMode, PoParser,
    };
    use crate::{InterchangeFormat, ParseMode};

    static NEXT_FILE: AtomicUsize = AtomicUsize::new(0);

    struct TestFile {
        path: std::path::PathBuf,
    }

    impl TestFile {
        fn new(extension: &str, content: &str) -> Self {
            let index = NEXT_FILE.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "lokit-po-{}-{index}.{extension}",
                std::process::id()
            ));
            fs::write(&path, content).expect("test PO should be written");
            Self { path }
        }
    }

    impl Drop for TestFile {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.path);
        }
    }

    #[test]
    fn parser_preserves_context_comments_and_plurals() {
        let input = TestFile::new(
            "po",
            r#"# Header
msgid ""
msgstr ""
"Language: fr\n"
"Plural-Forms: nplurals=2; plural=(n > 1);\n"

# Translator
#. Extracted
#: src/main.py:7
#, fuzzy, python-format
msgctxt "menu"
msgid "File"
msgid_plural "Files"
msgstr[0] "Fichier"
msgstr[1] "Fichiers"
"#,
        );
        let mut parser =
            PoParser::open(&input.path, Some("en".to_owned()), None, PoImportMode::Auto)
                .expect("PO should parse");
        assert_eq!(parser.metadata.target_locale.as_deref(), Some("fr"));
        let records = parser.read_batch(8).expect("records should parse");
        assert_eq!(records.len(), 2);
        assert_eq!(records[0].0, "po-0");
        assert_eq!(records[0].1.source, "File");
        assert_eq!(records[0].1.target.as_deref(), Some("Fichier"));
        assert_eq!(records[1].1.target.as_deref(), Some("Fichiers"));
        assert_eq!(records[0].1.comments.len(), 2);
        assert_eq!(
            records[0].1.comments[1].context_key.as_deref(),
            Some("menu")
        );
        assert_eq!(
            records[0]
                .1
                .plural
                .as_ref()
                .and_then(|plural| plural.category),
            Some(PluralCategory::One)
        );
        assert_eq!(
            records[1]
                .1
                .plural
                .as_ref()
                .and_then(|plural| plural.category),
            None
        );
    }

    #[test]
    fn parser_defers_an_error_until_after_completed_entries() {
        let input = TestFile::new(
            "po",
            r#"msgid "Valid"
msgstr "Valide"

this is not valid PO syntax
"#,
        );
        let mut parser = PoParser::open(
            &input.path,
            Some("en".to_owned()),
            Some("fr".to_owned()),
            PoImportMode::Gettext,
        )
        .expect("the valid first entry should initialize the parser");

        let batch = parser.read_batch_preserving_prefix(8);

        assert_eq!(batch.records.len(), 1);
        assert_eq!(batch.records[0].0, "Valid");
        assert_eq!(batch.records[0].1.source, "Valid");
        assert!(batch.error.is_some());
        assert!(!batch.exhausted);
    }

    #[test]
    fn oversized_physical_line_is_bounded_after_a_valid_prefix() {
        let input = TestFile::new(
            "po",
            &format!(
                "msgid \"Valid\"\nmsgstr \"Valide\"\n\n#. {}",
                "x".repeat(64),
            ),
        );
        let mut parser = PoParser::open(
            &input.path,
            Some("en".to_owned()),
            Some("fr".to_owned()),
            PoImportMode::Gettext,
        )
        .expect("the valid first entry should initialize the parser");
        parser.max_line_bytes = 32;

        let batch = parser.read_batch_preserving_prefix(8);

        assert_eq!(batch.records.len(), 1);
        assert_eq!(batch.records[0].0, "Valid");
        let error = batch.error.expect("the oversized line should fail");
        let message = error.to_string();
        assert!(message.contains(&format!("{}:4:", input.path.display())));
        assert!(message.contains("physical line exceeds the 32-byte content limit"));
    }

    #[test]
    fn oversized_entry_is_bounded_after_a_valid_prefix() {
        let input = TestFile::new(
            "po",
            concat!(
                "msgid \"Valid\"\n",
                "msgstr \"Valide\"\n",
                "\n",
                "#. 1234567890\n",
                "#. abcdefghij\n",
                "msgid \"Too large\"\n",
                "msgstr \"\"\n",
            ),
        );
        let mut parser = PoParser::open(
            &input.path,
            Some("en".to_owned()),
            Some("fr".to_owned()),
            PoImportMode::Gettext,
        )
        .expect("the valid first entry should initialize the parser");
        parser.max_entry_bytes = 32;

        let batch = parser.read_batch_preserving_prefix(8);

        assert_eq!(batch.records.len(), 1);
        assert_eq!(batch.records[0].0, "Valid");
        let error = batch.error.expect("the oversized entry should fail");
        let message = error.to_string();
        assert!(message.contains(&format!("{}:6:", input.path.display())));
        assert!(message.contains("entry starting on line 4"));
        assert!(message.contains("32-byte cumulative raw-input limit"));
    }

    #[test]
    fn configured_po_limits_accept_exact_boundaries() {
        let exact_line = format!("msgid \"{}\"", "x".repeat(24));
        assert_eq!(exact_line.len(), 32);
        let second_entry = format!("{exact_line}\nmsgstr \"\"\n");
        let input = TestFile::new(
            "po",
            &format!("msgid \"Valid\"\nmsgstr \"Valide\"\n\n{second_entry}"),
        );
        let mut parser = PoParser::open(
            &input.path,
            Some("en".to_owned()),
            Some("fr".to_owned()),
            PoImportMode::Gettext,
        )
        .expect("the valid first entry should initialize the parser");
        parser.max_line_bytes = exact_line.len();
        parser.max_entry_bytes = second_entry.len();

        let batch = parser.read_batch_preserving_prefix(8);

        assert!(batch.error.is_none());
        assert_eq!(batch.records.len(), 2);
        assert_eq!(batch.records[0].0, "Valid");
        assert_eq!(batch.records[1].0, "x".repeat(24));
    }

    #[test]
    fn auto_mode_recognizes_uppercase_pot_extension() {
        let input = TestFile::new("POT", "msgid \"Hello\"\nmsgstr \"Bonjour\"\n");
        let mut parser = PoParser::open(
            &input.path,
            Some("en".to_owned()),
            Some("fr".to_owned()),
            PoImportMode::Auto,
        )
        .expect("POT should parse");
        assert_eq!(parser.mode, PoImportMode::Source);
        let records = parser.read_batch(1).expect("record should parse");
        assert_eq!(records[0].1.target, None);
    }

    #[test]
    fn direct_xml_export_never_serializes_gettext_eot_separator() {
        let input = TestFile::new(
            "po",
            "msgid \"\"\nmsgstr \"\"\n\"Language: fr\\n\"\n\nmsgctxt \"menu\"\nmsgid \"File\"\nmsgstr \"Fichier\"\n",
        );
        let output = TestFile::new("xliff", "");
        convert_po_to_interchange(
            &input.path,
            &output.path,
            InterchangeFormat::Xliff,
            Some("en".to_owned()),
            None,
            PoImportMode::Auto,
        )
        .expect("PO should export");
        let xml = fs::read_to_string(&output.path).expect("XLIFF should be readable");
        assert!(!xml.contains('\u{4}'));
        assert!(xml.contains("x-po-msgctxt"));
        assert!(xml.contains("menu"));
    }

    #[test]
    fn direct_round_trip_omits_empty_targets_and_preserves_fuzzy_plurals() {
        let input = TestFile::new(
            "po",
            r#"msgid ""
msgstr ""
"Language: fr\n"
"Plural-Forms: nplurals=2; plural=(n > 1);\n"

#. Empty translation
msgid "Empty"
msgstr ""

#, fuzzy, python-format
msgctxt "files"
msgid "%d file"
msgid_plural "%d files"
msgstr[0] ""
msgstr[1] "%d fichiers"
"#,
        );
        for format in [InterchangeFormat::Tmx, InterchangeFormat::Xliff] {
            let extension = match format {
                InterchangeFormat::Tmx => "tmx",
                InterchangeFormat::Xliff => "xliff",
            };
            let interchange = TestFile::new(extension, "");
            let output = TestFile::new("po", "");
            convert_po_to_interchange(
                &input.path,
                &interchange.path,
                format,
                Some("en".to_owned()),
                None,
                PoImportMode::Gettext,
            )
            .expect("PO should export");
            let xml = fs::read_to_string(&interchange.path).expect("XML should be readable");
            let target_count = match format {
                InterchangeFormat::Tmx => xml.matches("xml:lang=\"fr\"").count(),
                InterchangeFormat::Xliff => xml.matches("<target").count(),
            };
            assert_eq!(target_count, 1);
            assert!(xml.contains("fuzzy"));

            assert_eq!(
                convert_interchange_to_po(
                    &interchange.path,
                    &output.path,
                    format,
                    None,
                    None,
                    ParseMode::Full,
                )
                .expect("interchange should convert"),
                Some(2)
            );
            let mut parser = PoParser::open(
                &output.path,
                Some("en".to_owned()),
                None,
                PoImportMode::Gettext,
            )
            .expect("round-tripped PO should parse");
            let (_, empty) = parser
                .next_data_entry()
                .expect("empty entry should parse")
                .expect("empty entry should exist");
            assert!(empty.translation.is_empty());
            let (_, plural) = parser
                .next_data_entry()
                .expect("plural entry should parse")
                .expect("plural entry should exist");
            assert_eq!(plural.context.as_deref(), Some("files"));
            assert_eq!(plural.id_plural, "%d files");
            assert_eq!(
                plural.plural_translations.get(&0).map(String::as_str),
                Some("")
            );
            assert_eq!(
                plural.plural_translations.get(&1).map(String::as_str),
                Some("%d fichiers")
            );
            assert!(plural.flags.iter().any(|flag| flag == "fuzzy"));
            assert_eq!(
                super::entry_status(&plural, PoImportMode::Gettext, 0),
                TranslationStatus::Draft
            );
        }
    }

    #[test]
    fn parser_makes_duplicate_unit_ids_collision_safe() {
        let input = TestFile::new(
            "po",
            "msgid \"Same\"\nmsgstr \"One\"\n\nmsgid \"Same\"\nmsgstr \"Two\"\n",
        );
        let mut parser = PoParser::open(
            &input.path,
            Some("en".to_owned()),
            Some("fr".to_owned()),
            PoImportMode::Gettext,
        )
        .expect("PO should parse");
        let records = parser.read_batch(8).expect("records should parse");
        assert_eq!(records[0].0, "Same");
        assert_eq!(records[1].0, "Same#2");
    }

    #[test]
    fn direct_po_export_preserves_non_gettext_unit_ids_without_exposing_metadata() {
        let entries = po_entries_from_units(
            vec![(
                "greeting/日本".to_owned(),
                Data {
                    source: "Hello world".to_owned(),
                    target: Some("Bonjour le monde".to_owned()),
                    ..Data::new(String::new())
                },
            )],
            Some("fr"),
            PoImportMode::Gettext,
            None,
        )
        .expect("PO entries should convert");

        assert_eq!(entries.len(), 1);
        assert_eq!(po_unit_id(&entries[0], 0), "greeting/日本");
        assert!(entry_comments(&entries[0]).is_empty());
    }

    #[test]
    fn interchange_generated_ids_do_not_become_po_comments() {
        let entries = po_entries_from_units(
            vec![(
                "po-0".to_owned(),
                Data {
                    source: "Empty".to_owned(),
                    extensions: vec![(super::PO_MSGID.to_owned(), "Empty".to_owned())],
                    ..Data::new(String::new())
                },
            )],
            Some("fr"),
            PoImportMode::Gettext,
            None,
        )
        .expect("PO entries should convert");

        assert_eq!(entries.len(), 1);
        assert!(entries[0]
            .extracted_comments
            .iter()
            .all(|comment| super::decode_lokit_unit_id_comment(comment).is_none()));
    }

    #[test]
    fn direct_export_rejects_late_invalid_xml_characters() {
        let input = TestFile::new(
            "po",
            "msgid \"Good\"\nmsgstr \"Bon\"\n\nmsgid \"Bad\\004value\"\nmsgstr \"\"\n",
        );
        let output = TestFile::new("xliff", "");
        let error = convert_po_to_interchange(
            &input.path,
            &output.path,
            InterchangeFormat::Xliff,
            Some("en".to_owned()),
            Some("fr".to_owned()),
            PoImportMode::Gettext,
        )
        .expect_err("invalid XML characters must fail");
        assert!(error.to_string().contains("XML 1.0"));
    }
}
