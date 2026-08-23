use std::collections::HashMap;
use std::fmt;
use std::fs::File;
use std::io::{self, BufReader, Read, Seek, SeekFrom};
use std::path::Path;

use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyTuple};
use quick_xml::events::{BytesStart, Event};
use quick_xml::reader::Reader as XmlReader;
use quick_xml::XmlVersion;

mod conversion;
mod lokit;
mod materialize;
mod placeholder;
mod po;

use lokit_format::id_registry::BoundedIdRegistry;

const READ_CAPACITY: usize = 64 * 1024;
const DEFAULT_BATCH_SIZE: usize = 256;
const MAX_BATCH_SIZE: usize = 16_384;
const MAX_BATCH_BYTES: usize = 16 * 1024 * 1024;
const MAX_COMPLEX_UNIT_BYTES: u64 = 64 * 1024 * 1024;
const MAX_RETAINED_EVENT_BUFFER_BYTES: usize = 1024 * 1024;
const UNKNOWN_STATUS: &str = "unknown";
const XLIFF_UNIT_NOTE_PREFIX: &str = "__lokit_native_xliff_unit_note.";

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum InterchangeFormat {
    Tmx,
    Xliff,
}

impl InterchangeFormat {
    fn parse(value: &str) -> NativeResult<Self> {
        match value {
            "tmx" => Ok(Self::Tmx),
            "xliff" => Ok(Self::Xliff),
            _ => Err(NativeError::Invalid(format!(
                "format_name must be 'tmx' or 'xliff', got {value:?}"
            ))),
        }
    }

    const fn root_name(self) -> &'static [u8] {
        match self {
            Self::Tmx => b"tmx",
            Self::Xliff => b"xliff",
        }
    }

    const fn unit_name(self) -> &'static [u8] {
        match self {
            Self::Tmx => b"tu",
            Self::Xliff => b"trans-unit",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum ParseMode {
    Full,
    Text,
    TextStatus,
}

impl ParseMode {
    fn parse(value: &str) -> NativeResult<Self> {
        match value {
            "full" => Ok(Self::Full),
            "text" => Ok(Self::Text),
            "text_status" => Ok(Self::TextStatus),
            _ => Err(NativeError::Invalid(format!(
                "mode must be 'full', 'text', or 'text_status', got {value:?}"
            ))),
        }
    }

    const fn includes_status(self) -> bool {
        matches!(self, Self::Full | Self::TextStatus)
    }
}

#[derive(Debug)]
enum NativeError {
    Invalid(String),
    Io(io::Error),
    Xml(quick_xml::Error),
}

impl fmt::Display for NativeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(message) => formatter.write_str(message),
            Self::Io(error) => write!(formatter, "{error}"),
            Self::Xml(error) => write!(formatter, "{error}"),
        }
    }
}

impl From<io::Error> for NativeError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<quick_xml::Error> for NativeError {
    fn from(error: quick_xml::Error) -> Self {
        Self::Xml(error)
    }
}

type NativeResult<T> = Result<T, NativeError>;

struct PrefixBatch<T> {
    records: Vec<T>,
    error: Option<NativeError>,
    exhausted: bool,
}

#[derive(Clone, Debug, Default)]
struct Metadata {
    version: String,
    source_locale: Option<String>,
    target_locale: Option<String>,
    source_language: Option<String>,
    target_language: Option<String>,
    target_locales: Vec<String>,
    target_languages: Vec<String>,
    export_origin: String,
    export_timestamp: String,
    extensions: HashMap<String, String>,
}

impl Metadata {
    fn set_source_locale(&mut self, locale: String) {
        if locale.is_empty() || locale == "*all*" {
            return;
        }
        if self.source_locale.is_none() {
            let locale = canonical_locale(&locale);
            self.source_language = Some(base_language(&locale));
            self.source_locale = Some(locale);
        }
    }

    fn add_target_locale(&mut self, locale: String) {
        if locale.is_empty() {
            return;
        }
        let locale = canonical_locale(&locale);
        if !self.target_locales.contains(&locale) {
            self.target_languages.push(base_language(&locale));
            self.target_locales.push(locale.clone());
        }
        if self.target_locales.len() == 1 {
            self.target_language = Some(base_language(&locale));
            self.target_locale = Some(locale);
        } else {
            self.target_language = None;
            self.target_locale = None;
        }
    }
}

#[derive(Clone, Debug, Default)]
struct XliffFileContext {
    index: usize,
    original: String,
    target_locale: Option<String>,
    data_type: String,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
enum TextField {
    #[default]
    None,
    Source,
    Target,
}

#[derive(Debug)]
struct UnitBuilder {
    unit_id: String,
    source: String,
    target: String,
    targets: Vec<(String, String)>,
    status: String,
    extensions: HashMap<String, String>,
    is_complex: bool,
    field: TextField,
    tmx_tuv_locale: Option<String>,
    tmx_status_property: bool,
    status_text: String,
    source_seen: bool,
    target_seen: bool,
}

impl UnitBuilder {
    fn new(unit_id: String, extensions: HashMap<String, String>) -> Self {
        Self {
            unit_id,
            source: String::new(),
            target: String::new(),
            targets: Vec::new(),
            status: UNKNOWN_STATUS.to_owned(),
            extensions,
            is_complex: false,
            field: TextField::None,
            tmx_tuv_locale: None,
            tmx_status_property: false,
            status_text: String::new(),
            source_seen: false,
            target_seen: false,
        }
    }
}

#[derive(Debug)]
struct NativeRecord {
    is_complex: bool,
    unit_id: String,
    source: String,
    target: Option<String>,
    targets: Vec<(String, String)>,
    status: String,
    extensions: HashMap<String, String>,
    fragment: Option<Vec<u8>>,
}

impl NativeRecord {
    fn retained_bytes(&self) -> usize {
        let mut bytes = std::mem::size_of::<Self>()
            .saturating_add(self.unit_id.capacity())
            .saturating_add(self.source.capacity())
            .saturating_add(self.target.as_ref().map_or(0, String::capacity))
            .saturating_add(self.status.capacity())
            .saturating_add(
                self.targets
                    .capacity()
                    .saturating_mul(std::mem::size_of::<(String, String)>()),
            )
            .saturating_add(
                self.extensions
                    .capacity()
                    .saturating_mul(std::mem::size_of::<(String, String)>()),
            )
            .saturating_add(self.fragment.as_ref().map_or(0, Vec::capacity));
        for (locale, target) in &self.targets {
            bytes = bytes
                .saturating_add(locale.capacity())
                .saturating_add(target.capacity());
        }
        for (key, value) in &self.extensions {
            bytes = bytes
                .saturating_add(key.capacity())
                .saturating_add(value.capacity());
        }
        bytes
    }
}

struct NativeParser {
    xml: XmlReader<BufReader<File>>,
    fragment_input: File,
    buffer: Vec<u8>,
    format: InterchangeFormat,
    mode: ParseMode,
    requested_source: Option<String>,
    requested_target: Option<String>,
    parse_header_metadata: bool,
    metadata: Metadata,
    file_context: Option<XliffFileContext>,
    next_file_index: usize,
    generated_unit_index: usize,
    used_unit_ids: BoundedIdRegistry,
    current: Option<UnitBuilder>,
    pending_record: Option<NativeRecord>,
    namespace_attributes: Vec<(Vec<u8>, Vec<u8>)>,
    current_unit_start: u64,
    event_start: u64,
    event_end: u64,
    in_tmx_header: bool,
    tmx_header_property_name: Option<Vec<u8>>,
    tmx_header_property_key: Option<String>,
    tmx_header_property_value: String,
    tmx_header_property_keep_empty: bool,
    xliff_v2: bool,
    xliff_unit_open: bool,
    xliff_unit_id: Option<String>,
    xliff_unit_notes: Vec<String>,
    xliff_unit_note_text: Option<String>,
    element_depth: usize,
    xml_declaration_allowed: bool,
    xml_declaration_seen: bool,
    doctype_seen: bool,
    root_seen: bool,
    root_closed: bool,
    validate_all_xml_chars: bool,
    eof: bool,
}

impl NativeParser {
    fn open(
        path: &Path,
        format: InterchangeFormat,
        source_language: Option<String>,
        target_language: Option<String>,
        mode: ParseMode,
    ) -> NativeResult<Self> {
        Self::open_with_xml_validation(path, format, source_language, target_language, mode, false)
    }

    fn open_with_xml_validation(
        path: &Path,
        format: InterchangeFormat,
        source_language: Option<String>,
        target_language: Option<String>,
        mode: ParseMode,
        validate_all_xml_chars: bool,
    ) -> NativeResult<Self> {
        let file = File::open(path)?;
        let fragment_input = File::open(path)?;
        let input = BufReader::with_capacity(READ_CAPACITY, file);
        let mut xml = XmlReader::from_reader(input);
        xml.config_mut().trim_text(false);
        let parse_header_metadata = format != InterchangeFormat::Tmx
            || source_language.is_none()
            || target_language.is_none();
        let mut metadata = Metadata::default();
        if parse_header_metadata {
            metadata.extensions.insert(
                "input_format".to_owned(),
                match format {
                    InterchangeFormat::Tmx => "tmx".to_owned(),
                    InterchangeFormat::Xliff => "xliff".to_owned(),
                },
            );
        }
        let source_language = source_language.map(|locale| canonical_locale(&locale));
        let target_language = target_language.map(|locale| canonical_locale(&locale));
        if let Some(locale) = source_language.as_ref() {
            metadata.set_source_locale(locale.clone());
        }
        if let Some(locale) = target_language.as_ref() {
            metadata.add_target_locale(locale.clone());
        }
        let mut parser = Self {
            xml,
            fragment_input,
            buffer: Vec::with_capacity(READ_CAPACITY),
            format,
            mode,
            requested_source: source_language,
            requested_target: target_language,
            parse_header_metadata,
            metadata,
            file_context: None,
            next_file_index: 0,
            generated_unit_index: 0,
            used_unit_ids: BoundedIdRegistry::default(),
            current: None,
            pending_record: None,
            namespace_attributes: Vec::new(),
            current_unit_start: 0,
            event_start: 0,
            event_end: 0,
            in_tmx_header: false,
            tmx_header_property_name: None,
            tmx_header_property_key: None,
            tmx_header_property_value: String::new(),
            tmx_header_property_keep_empty: false,
            xliff_v2: false,
            xliff_unit_open: false,
            xliff_unit_id: None,
            xliff_unit_notes: Vec::new(),
            xliff_unit_note_text: None,
            element_depth: 0,
            xml_declaration_allowed: true,
            xml_declaration_seen: false,
            doctype_seen: false,
            root_seen: false,
            root_closed: false,
            validate_all_xml_chars,
            eof: false,
        };
        parser.prepare_preamble()?;
        parser.release_oversized_event_buffer();
        Ok(parser)
    }

    fn prepare_preamble(&mut self) -> NativeResult<()> {
        while self.current.is_none() && !self.eof {
            self.process_next_event()?;
        }
        if !self.root_seen {
            return Err(NativeError::Invalid(
                "XML document does not contain the expected root element".to_owned(),
            ));
        }
        Ok(())
    }

    fn read_batch(&mut self, batch_size: usize) -> NativeResult<Vec<NativeRecord>> {
        let batch = self.read_batch_preserving_prefix(batch_size);
        match batch.error {
            Some(error) => Err(error),
            None => Ok(batch.records),
        }
    }

    fn read_batch_preserving_prefix(&mut self, batch_size: usize) -> PrefixBatch<NativeRecord> {
        self.read_batch_with_byte_budget(batch_size, MAX_BATCH_BYTES)
    }

    fn read_batch_with_byte_budget(
        &mut self,
        batch_size: usize,
        byte_budget: usize,
    ) -> PrefixBatch<NativeRecord> {
        let mut records = Vec::with_capacity(batch_size.min(DEFAULT_BATCH_SIZE));
        let mut retained_bytes = 0usize;
        let mut error = None;
        while records.len() < batch_size {
            let record = if let Some(record) = self.pending_record.take() {
                Some(record)
            } else if self.eof {
                None
            } else {
                match self.process_next_event() {
                    Ok(record) => record,
                    Err(failure) => {
                        error = Some(failure);
                        break;
                    }
                }
            };
            let Some(record) = record else {
                if self.eof {
                    break;
                }
                continue;
            };
            let record_bytes = record.retained_bytes();
            if !records.is_empty() && retained_bytes.saturating_add(record_bytes) > byte_budget {
                self.pending_record = Some(record);
                break;
            }
            retained_bytes = retained_bytes.saturating_add(record_bytes);
            records.push(record);
            if retained_bytes >= byte_budget {
                break;
            }
        }
        self.release_oversized_event_buffer();
        PrefixBatch {
            records,
            error,
            exhausted: self.is_exhausted(),
        }
    }

    fn is_exhausted(&self) -> bool {
        self.eof && self.pending_record.is_none()
    }

    fn release_oversized_event_buffer(&mut self) {
        self.buffer.clear();
        if self.buffer.capacity() > MAX_RETAINED_EVENT_BUFFER_BYTES {
            self.buffer = Vec::with_capacity(READ_CAPACITY);
        }
    }

    fn process_next_event(&mut self) -> NativeResult<Option<NativeRecord>> {
        let mut buffer = std::mem::take(&mut self.buffer);
        buffer.clear();
        self.event_start = self.xml.buffer_position();
        let event = self.xml.read_event_into(&mut buffer)?;
        self.event_end = self.xml.buffer_position();
        let result = self.process_event(event);
        self.buffer = buffer;
        result
    }

    fn process_event(&mut self, event: Event<'_>) -> NativeResult<Option<NativeRecord>> {
        match event {
            Event::Start(element) => {
                if self.root_closed {
                    return Err(NativeError::Invalid(
                        "XML document contains an element after the root element".to_owned(),
                    ));
                }
                if self.validate_all_xml_chars {
                    validate_element_xml_chars(&element, self.xml.decoder())?;
                }
                self.xml_declaration_allowed = false;
                let result = self.process_start(&element);
                if result.is_ok() {
                    self.element_depth += 1;
                }
                result
            }
            Event::Empty(element) => {
                if self.root_closed {
                    return Err(NativeError::Invalid(
                        "XML document contains an element after the root element".to_owned(),
                    ));
                }
                if self.validate_all_xml_chars {
                    validate_element_xml_chars(&element, self.xml.decoder())?;
                }
                self.xml_declaration_allowed = false;
                let root_seen = self.root_seen;
                let result = self.process_empty(&element);
                if result.is_ok() && !root_seen && self.root_seen {
                    self.root_closed = true;
                }
                result
            }
            Event::End(element) => {
                if self.root_closed || self.element_depth == 0 {
                    return Err(NativeError::Invalid(
                        "XML document contains an unexpected closing element".to_owned(),
                    ));
                }
                self.element_depth -= 1;
                if self.element_depth == 0 {
                    self.root_closed = true;
                }
                let local_name = element.local_name();
                let name = local_name.as_ref();
                if self.current.is_some() && name == self.translation_unit_name() {
                    return self.finish_unit().map(Some);
                }
                self.process_end(name);
                Ok(None)
            }
            Event::Text(text) => {
                let decoded = text.decode().map_err(|error| {
                    NativeError::Invalid(format!("cannot decode XML text: {error}"))
                })?;
                let value = quick_xml::escape::unescape(&decoded).map_err(|error| {
                    NativeError::Invalid(format!("cannot unescape XML text: {error}"))
                })?;
                validate_xml_1_0_chars(&value, "XML text")?;
                if !self.root_seen {
                    self.xml_declaration_allowed = false;
                }
                if (!self.root_seen || self.root_closed) && !is_xml_whitespace(&value) {
                    return Err(NativeError::Invalid(
                        "XML document contains text outside the root element".to_owned(),
                    ));
                }
                if self.root_seen && !self.root_closed {
                    self.process_text(&value);
                }
                Ok(None)
            }
            Event::CData(text) => {
                if !self.root_seen || self.root_closed {
                    return Err(NativeError::Invalid(
                        "XML document contains CDATA outside the root element".to_owned(),
                    ));
                }
                let value = text.decode().map_err(|error| {
                    NativeError::Invalid(format!("cannot decode XML CDATA: {error}"))
                })?;
                validate_xml_1_0_chars(&value, "XML CDATA")?;
                self.process_text(&value);
                Ok(None)
            }
            Event::GeneralRef(reference) => {
                if !self.root_seen || self.root_closed {
                    return Err(NativeError::Invalid(
                        "XML document contains an entity reference outside the root element"
                            .to_owned(),
                    ));
                }
                let value = if let Some(character) =
                    reference.resolve_char_ref().map_err(|error| {
                        NativeError::Invalid(format!("invalid XML character reference: {error}"))
                    })? {
                    validate_xml_1_0_char(character, "XML character reference")?;
                    character.to_string()
                } else {
                    let name = reference.decode().map_err(|error| {
                        NativeError::Invalid(format!("cannot decode XML entity: {error}"))
                    })?;
                    quick_xml::escape::resolve_predefined_entity(&name)
                        .ok_or_else(|| {
                            NativeError::Invalid(format!(
                                "unresolved XML entity reference: &{name};"
                            ))
                        })?
                        .to_owned()
                };
                self.process_text(&value);
                Ok(None)
            }
            Event::Eof => {
                if self.current.is_some() {
                    return Err(NativeError::Invalid(
                        "XML document ended inside a translation unit".to_owned(),
                    ));
                }
                if self.root_seen && (!self.root_closed || self.element_depth != 0) {
                    return Err(NativeError::Invalid(
                        "XML document ended before the root element was closed".to_owned(),
                    ));
                }
                self.eof = true;
                Ok(None)
            }
            Event::Decl(_) => {
                if self.root_seen || self.xml_declaration_seen || !self.xml_declaration_allowed {
                    return Err(NativeError::Invalid(
                        "XML declaration must be the first token and appear only once".to_owned(),
                    ));
                }
                self.xml_declaration_seen = true;
                self.xml_declaration_allowed = false;
                Ok(None)
            }
            Event::DocType(doctype) => {
                if self.root_seen || self.doctype_seen {
                    return Err(NativeError::Invalid(
                        "XML doctype must appear before the root element and only once".to_owned(),
                    ));
                }
                if self.validate_all_xml_chars {
                    let value = doctype.decode().map_err(|error| {
                        NativeError::Invalid(format!("cannot decode XML doctype: {error}"))
                    })?;
                    validate_xml_1_0_chars(&value, "XML doctype")?;
                }
                self.doctype_seen = true;
                self.xml_declaration_allowed = false;
                Ok(None)
            }
            Event::Comment(comment) => {
                if self.validate_all_xml_chars {
                    let value = comment.decode().map_err(|error| {
                        NativeError::Invalid(format!("cannot decode XML comment: {error}"))
                    })?;
                    validate_xml_1_0_chars(&value, "XML comment")?;
                }
                if !self.root_seen {
                    self.xml_declaration_allowed = false;
                }
                Ok(None)
            }
            Event::PI(instruction) => {
                if self.validate_all_xml_chars {
                    let value =
                        self.xml
                            .decoder()
                            .decode(instruction.as_ref())
                            .map_err(|error| {
                                NativeError::Invalid(format!(
                                    "cannot decode XML processing instruction: {error}"
                                ))
                            })?;
                    validate_xml_1_0_chars(&value, "XML processing instruction")?;
                }
                if !self.root_seen {
                    self.xml_declaration_allowed = false;
                }
                Ok(None)
            }
        }
    }

    fn process_start(&mut self, element: &BytesStart<'_>) -> NativeResult<Option<NativeRecord>> {
        if self.current.is_none() {
            self.remember_namespaces(element)?;
        }
        let local_name = element.local_name();
        let name = local_name.as_ref();
        if !self.root_seen {
            self.initialize_root(element, name)?;
            return Ok(None);
        }
        if self.current.is_none()
            && self.xliff_v2
            && self.format == InterchangeFormat::Xliff
            && name == b"unit"
        {
            self.initialize_xliff_unit(element)?;
            return Ok(None);
        }
        if self.current.is_none() && name == self.translation_unit_name() {
            self.current_unit_start = self.event_start;
            self.begin_unit(element)?;
            return Ok(None);
        }
        if self.current.is_some() {
            self.process_unit_start(element, name)?;
        } else {
            self.process_preamble_start(element, name)?;
        }
        Ok(None)
    }

    fn process_empty(&mut self, element: &BytesStart<'_>) -> NativeResult<Option<NativeRecord>> {
        if self.current.is_none() {
            self.remember_namespaces(element)?;
        }
        let local_name = element.local_name();
        let name = local_name.as_ref();
        if !self.root_seen {
            self.initialize_root(element, name)?;
            return Ok(None);
        }
        if self.current.is_none()
            && self.xliff_v2
            && self.format == InterchangeFormat::Xliff
            && name == b"unit"
        {
            self.initialize_xliff_unit(element)?;
            self.xliff_unit_open = false;
            self.xliff_unit_id = None;
            self.xliff_unit_notes.clear();
            return Ok(None);
        }
        if self.current.is_none() && name == self.translation_unit_name() {
            self.current_unit_start = self.event_start;
            self.begin_unit(element)?;
            return self.finish_unit().map(Some);
        }
        if self.current.is_some() {
            self.process_unit_empty(element, name)?;
        } else {
            self.process_preamble_empty(element, name)?;
        }
        Ok(None)
    }

    fn initialize_root(&mut self, element: &BytesStart<'_>, name: &[u8]) -> NativeResult<()> {
        if name != self.format.root_name() {
            return Err(NativeError::Invalid(format!(
                "expected {} XML root, found {}",
                String::from_utf8_lossy(self.format.root_name()).to_uppercase(),
                String::from_utf8_lossy(name)
            )));
        }
        self.root_seen = true;
        let default_version = match self.format {
            InterchangeFormat::Tmx => "1.4",
            InterchangeFormat::Xliff => "1.2",
        };
        let version = attribute_value(element, b"version", self.xml.decoder())?
            .unwrap_or_else(|| default_version.to_owned());
        self.xliff_v2 = self.format == InterchangeFormat::Xliff && !version.starts_with('1');
        self.metadata.version = version.clone();
        if self.format == InterchangeFormat::Xliff {
            self.metadata
                .extensions
                .insert("xliff_version".to_owned(), version);
            if self.xliff_v2 {
                if let Some(locale) = attribute_value(element, b"srcLang", self.xml.decoder())? {
                    self.metadata.set_source_locale(locale);
                }
                if let Some(locale) = attribute_value(element, b"trgLang", self.xml.decoder())? {
                    self.metadata.add_target_locale(locale);
                }
            }
        }
        Ok(())
    }

    fn process_preamble_start(
        &mut self,
        element: &BytesStart<'_>,
        name: &[u8],
    ) -> NativeResult<()> {
        match self.format {
            InterchangeFormat::Tmx if name == b"header" => {
                self.in_tmx_header = true;
                self.initialize_tmx_header(element)
            }
            InterchangeFormat::Tmx if self.in_tmx_header => {
                self.initialize_tmx_header_property(element, name)
            }
            InterchangeFormat::Xliff
                if self.xliff_v2 && self.xliff_unit_open && name == b"note" =>
            {
                self.xliff_unit_note_text = Some(String::new());
                Ok(())
            }
            InterchangeFormat::Xliff if name == b"file" => self.initialize_xliff_file(element),
            _ => Ok(()),
        }
    }

    fn process_preamble_empty(
        &mut self,
        element: &BytesStart<'_>,
        name: &[u8],
    ) -> NativeResult<()> {
        self.process_preamble_start(element, name)?;
        self.process_end(name);
        Ok(())
    }

    fn initialize_tmx_header(&mut self, element: &BytesStart<'_>) -> NativeResult<()> {
        if !self.parse_header_metadata {
            return Ok(());
        }
        if self.requested_source.is_none() {
            if let Some(locale) = attribute_value(element, b"srclang", self.xml.decoder())? {
                self.metadata.set_source_locale(locale);
            }
        }
        if self.requested_target.is_none() {
            if let Some(locale) = attribute_value(element, b"tgtlang", self.xml.decoder())? {
                self.metadata.add_target_locale(locale);
            }
        }
        let tool = attribute_value(element, b"creationtool", self.xml.decoder())?
            .unwrap_or_else(|| "unknown_origin".to_owned());
        let tool_version = attribute_value(element, b"creationtoolversion", self.xml.decoder())?
            .unwrap_or_default();
        self.metadata.export_origin = format!("{tool} {tool_version}").trim().to_owned();
        self.metadata.export_timestamp =
            attribute_value(element, b"creationdate", self.xml.decoder())?.unwrap_or_default();
        for (attribute, extension) in [
            (b"adminlang".as_slice(), "admin_locale"),
            (b"datatype".as_slice(), "data_type"),
            (b"segtype".as_slice(), "segmentation"),
            (b"o-tmf".as_slice(), "translation_memory_format"),
        ] {
            if let Some(value) = attribute_value(element, attribute, self.xml.decoder())? {
                self.metadata.extensions.insert(extension.to_owned(), value);
            }
        }
        self.metadata
            .extensions
            .insert("tool_name".to_owned(), tool);
        if !tool_version.is_empty() {
            self.metadata
                .extensions
                .insert("tool_version".to_owned(), tool_version);
        }
        Ok(())
    }

    fn initialize_xliff_file(&mut self, element: &BytesStart<'_>) -> NativeResult<()> {
        let index = self.next_file_index;
        self.next_file_index += 1;
        let source_locale = attribute_value(element, b"source-language", self.xml.decoder())?
            .or_else(|| self.metadata.source_locale.clone())
            .unwrap_or_default();
        let target_locale = attribute_value(element, b"target-language", self.xml.decoder())?
            .or_else(|| self.requested_target.clone())
            .or_else(|| self.metadata.target_locale.clone());
        self.metadata.set_source_locale(source_locale.clone());
        if let Some(locale) = target_locale.as_ref() {
            self.metadata.add_target_locale(locale.clone());
        }
        self.file_context = Some(XliffFileContext {
            index,
            original: attribute_value(element, b"original", self.xml.decoder())?
                .or_else(|| {
                    attribute_value(element, b"id", self.xml.decoder())
                        .ok()
                        .flatten()
                })
                .unwrap_or_default(),
            target_locale,
            data_type: attribute_value(element, b"datatype", self.xml.decoder())?
                .unwrap_or_default(),
        });
        Ok(())
    }

    fn initialize_xliff_unit(&mut self, element: &BytesStart<'_>) -> NativeResult<()> {
        self.xliff_unit_open = true;
        self.xliff_unit_id = attribute_value(element, b"id", self.xml.decoder())?;
        self.xliff_unit_notes.clear();
        self.xliff_unit_note_text = None;
        Ok(())
    }

    fn initialize_tmx_header_property(
        &mut self,
        element: &BytesStart<'_>,
        name: &[u8],
    ) -> NativeResult<()> {
        if !self.parse_header_metadata || self.tmx_header_property_key.is_some() {
            return Ok(());
        }
        let (key, keep_empty) = if name == b"prop" {
            let property_type = attribute_value(element, b"type", self.xml.decoder())?
                .unwrap_or_else(|| "unknown".to_owned());
            (normalize_extension_key(&property_type), true)
        } else {
            (
                normalize_extension_key(&String::from_utf8_lossy(name)),
                false,
            )
        };
        self.tmx_header_property_name = Some(name.to_vec());
        self.tmx_header_property_key = Some(format!("property.{key}"));
        self.tmx_header_property_value.clear();
        self.tmx_header_property_keep_empty = keep_empty;
        Ok(())
    }

    fn translation_unit_name(&self) -> &[u8] {
        if self.xliff_v2 && self.format == InterchangeFormat::Xliff {
            b"segment"
        } else {
            self.format.unit_name()
        }
    }

    fn begin_unit(&mut self, element: &BytesStart<'_>) -> NativeResult<()> {
        let has_xliff_unit_notes = self.xliff_v2 && !self.xliff_unit_notes.is_empty();
        let (unit_id, extensions, has_unhandled_attributes) = match self.format {
            InterchangeFormat::Tmx => {
                let raw_id =
                    attribute_value(element, b"tuid", self.xml.decoder())?.unwrap_or_default();
                let preferred_id = if raw_id.is_empty() {
                    let generated = format!("auto_{}", self.generated_unit_index);
                    self.generated_unit_index += 1;
                    generated
                } else {
                    raw_id.clone()
                };
                let unit_id = self.unique_tmx_unit_id(preferred_id)?;
                (
                    unit_id,
                    HashMap::from([("unit_id".to_owned(), raw_id)]),
                    has_attributes_other_than(element, &[b"tuid".as_slice()]),
                )
            }
            InterchangeFormat::Xliff => {
                let context = self.file_context.clone().unwrap_or_default();
                let element_id =
                    attribute_value(element, b"id", self.xml.decoder())?.unwrap_or_default();
                let parent_id = self.xliff_unit_id.clone().unwrap_or_default();
                let preferred_id = if self.xliff_v2 {
                    if element_id.is_empty() {
                        if parent_id.is_empty() {
                            context.index.to_string()
                        } else {
                            parent_id.clone()
                        }
                    } else if parent_id.is_empty() {
                        element_id.clone()
                    } else {
                        format!("{parent_id}:{element_id}")
                    }
                } else if element_id.is_empty() {
                    context.index.to_string()
                } else {
                    element_id.clone()
                };
                let unit_id = self.unique_xliff_unit_id(preferred_id, context.index)?;
                let mut extensions = HashMap::from([
                    ("resource".to_owned(), context.original),
                    ("resource_index".to_owned(), context.index.to_string()),
                    (
                        "unit_id".to_owned(),
                        if self.xliff_v2 {
                            parent_id
                        } else {
                            element_id.clone()
                        },
                    ),
                ]);
                if self.xliff_v2 && !element_id.is_empty() {
                    extensions.insert("segment_id".to_owned(), element_id);
                }
                if self.xliff_v2 {
                    extensions.insert("xliff_version".to_owned(), self.metadata.version.clone());
                    for (index, note) in self.xliff_unit_notes.iter().enumerate() {
                        extensions.insert(format!("{XLIFF_UNIT_NOTE_PREFIX}{index}"), note.clone());
                    }
                }
                if !context.data_type.is_empty() {
                    extensions.insert("data_type".to_owned(), context.data_type);
                }
                if let Some(space) = attribute_value(element, b"space", self.xml.decoder())? {
                    extensions.insert("space".to_owned(), space);
                }
                let has_unhandled_attributes = if self.xliff_v2 {
                    has_attributes_other_than(
                        element,
                        &[
                            b"id".as_slice(),
                            b"space".as_slice(),
                            b"state".as_slice(),
                            b"subState".as_slice(),
                            b"canResegment".as_slice(),
                        ],
                    )
                } else {
                    has_attributes_other_than(element, &[b"id".as_slice(), b"space".as_slice()])
                };
                (unit_id, extensions, has_unhandled_attributes)
            }
        };
        let mut unit = UnitBuilder::new(unit_id, extensions);
        if self.xliff_v2 && self.format == InterchangeFormat::Xliff && self.mode.includes_status() {
            let state = attribute_value(element, b"state", self.xml.decoder())?.unwrap_or_default();
            unit.status = xliff_v2_status(&state).to_owned();
        }
        if self.mode == ParseMode::Full && (has_unhandled_attributes || has_xliff_unit_notes) {
            unit.is_complex = true;
        }
        self.current = Some(unit);
        Ok(())
    }

    fn unique_xliff_unit_id(
        &mut self,
        preferred: String,
        resource_index: usize,
    ) -> NativeResult<String> {
        if self.used_unit_ids.insert(&preferred)? {
            return Ok(preferred);
        }
        let scoped = format!("{resource_index}:{preferred}");
        if self.used_unit_ids.insert(&scoped)? {
            return Ok(scoped);
        }
        loop {
            let suffix = self.used_unit_ids.next_suffix(&scoped)?;
            let candidate = format!("{scoped}#{suffix}");
            if self.used_unit_ids.insert(&candidate)? {
                return Ok(candidate);
            }
        }
    }

    fn unique_tmx_unit_id(&mut self, preferred: String) -> NativeResult<String> {
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

    fn process_unit_start(&mut self, element: &BytesStart<'_>, name: &[u8]) -> NativeResult<()> {
        match self.format {
            InterchangeFormat::Tmx => self.process_tmx_unit_start(element, name),
            InterchangeFormat::Xliff => self.process_xliff_unit_start(element, name),
        }
    }

    fn process_unit_empty(&mut self, element: &BytesStart<'_>, name: &[u8]) -> NativeResult<()> {
        self.process_unit_start(element, name)?;
        self.process_end(name);
        Ok(())
    }

    fn process_tmx_unit_start(
        &mut self,
        element: &BytesStart<'_>,
        name: &[u8],
    ) -> NativeResult<()> {
        let decoder = self.xml.decoder();
        match name {
            b"tuv" => {
                let locale = attribute_value(element, b"lang", decoder)?
                    .map(|value| canonical_locale(&value));
                if self.metadata.source_locale.is_none() {
                    if let Some(locale) = locale.as_ref() {
                        self.metadata.set_source_locale(locale.clone());
                    }
                } else if self.requested_target.is_none() {
                    if let Some(locale) = locale.as_ref() {
                        if !same_locale(locale, self.source_locale_for_matching()) {
                            self.metadata.add_target_locale(locale.clone());
                        }
                    }
                }
                let unit = self.current.as_mut().expect("unit exists");
                unit.tmx_tuv_locale = locale;
                if unit.tmx_tuv_locale.is_none() {
                    unit.is_complex = true;
                }
            }
            b"seg" => {
                let locale = self
                    .current
                    .as_ref()
                    .and_then(|unit| unit.tmx_tuv_locale.as_deref())
                    .unwrap_or_default();
                let field = if same_locale(locale, self.source_locale_for_matching()) {
                    TextField::Source
                } else if self
                    .requested_target
                    .as_deref()
                    .is_none_or(|target| same_locale(locale, target))
                {
                    TextField::Target
                } else {
                    TextField::None
                };
                let unit = self.current.as_mut().expect("unit exists");
                unit.field = field;
                unit.source_seen |= field == TextField::Source;
                unit.target_seen |= field == TextField::Target;
            }
            b"prop" => {
                let unit = self.current.as_mut().expect("unit exists");
                if self.mode == ParseMode::Full {
                    unit.is_complex = true;
                }
                let property_type = attribute_value(element, b"type", decoder)?
                    .unwrap_or_default()
                    .to_ascii_lowercase();
                unit.tmx_status_property = is_status_property(&property_type);
                unit.status_text.clear();
            }
            b"note" => {
                let unit = self.current.as_mut().expect("unit exists");
                if self.mode == ParseMode::Full {
                    unit.is_complex = true;
                }
            }
            _ if self
                .current
                .as_ref()
                .is_some_and(|unit| unit.field != TextField::None) =>
            {
                self.current.as_mut().expect("unit exists").is_complex = true;
            }
            _ => {
                if self.mode == ParseMode::Full {
                    self.current.as_mut().expect("unit exists").is_complex = true;
                }
            }
        }
        Ok(())
    }

    fn process_xliff_unit_start(
        &mut self,
        element: &BytesStart<'_>,
        name: &[u8],
    ) -> NativeResult<()> {
        let decoder = self.xml.decoder();
        let unit = self.current.as_mut().expect("unit exists");
        match name {
            b"source" => {
                unit.source_seen = true;
                unit.field = TextField::Source;
            }
            b"target" => {
                unit.target_seen = true;
                unit.field = TextField::Target;
                if self.mode.includes_status() && !self.xliff_v2 {
                    let state = attribute_value(element, b"state", decoder)?.unwrap_or_default();
                    unit.status = xliff_status(&state).to_owned();
                }
            }
            b"note" => {
                if self.mode == ParseMode::Full {
                    unit.is_complex = true;
                }
            }
            _ if unit.field != TextField::None => unit.is_complex = true,
            _ => {
                if self.mode == ParseMode::Full {
                    unit.is_complex = true;
                }
            }
        }
        Ok(())
    }

    fn process_end(&mut self, name: &[u8]) {
        if self.current.is_none() {
            if self.format == InterchangeFormat::Tmx && self.in_tmx_header {
                self.finish_tmx_header_property(name);
            }
            if self.xliff_v2 && self.xliff_unit_open && name == b"note" {
                if let Some(note) = self.xliff_unit_note_text.take() {
                    if !note.is_empty() {
                        self.xliff_unit_notes.push(note.trim().to_owned());
                    }
                }
            }
            if self.format == InterchangeFormat::Tmx && name == b"header" {
                self.in_tmx_header = false;
            }
            if self.format == InterchangeFormat::Xliff && name == b"file" {
                self.file_context = None;
            }
            if self.xliff_v2 && self.format == InterchangeFormat::Xliff && name == b"unit" {
                self.xliff_unit_open = false;
                self.xliff_unit_id = None;
                self.xliff_unit_notes.clear();
                self.xliff_unit_note_text = None;
            }
            return;
        }
        let completed_target_locale = if self.format == InterchangeFormat::Tmx
            && name == b"tuv"
            && self.requested_target.is_none()
        {
            self.current
                .as_ref()
                .and_then(|unit| unit.tmx_tuv_locale.as_deref())
                .filter(|locale| !same_locale(locale, self.source_locale_for_matching()))
                .map(str::to_owned)
        } else {
            None
        };
        let unit = self.current.as_mut().expect("unit exists");
        match self.format {
            InterchangeFormat::Tmx => match name {
                b"seg" => unit.field = TextField::None,
                b"tuv" => {
                    unit.field = TextField::None;
                    if let Some(locale) = completed_target_locale {
                        unit.targets
                            .push((locale, std::mem::take(&mut unit.target)));
                    }
                    unit.tmx_tuv_locale = None;
                }
                b"prop" => {
                    if self.mode.includes_status() && unit.tmx_status_property {
                        unit.status = tmx_status(unit.status_text.trim()).to_owned();
                    }
                    unit.tmx_status_property = false;
                    unit.status_text.clear();
                }
                _ => {}
            },
            InterchangeFormat::Xliff => {
                if name == b"source" || name == b"target" {
                    unit.field = TextField::None;
                }
            }
        }
    }

    fn process_text(&mut self, value: &str) {
        if let Some(note) = self.xliff_unit_note_text.as_mut() {
            note.push_str(value);
            return;
        }
        if self.tmx_header_property_key.is_some() {
            self.tmx_header_property_value.push_str(value);
            return;
        }
        let Some(unit) = self.current.as_mut() else {
            return;
        };
        match unit.field {
            TextField::Source => unit.source.push_str(value),
            TextField::Target => unit.target.push_str(value),
            TextField::None => {
                if unit.tmx_status_property {
                    unit.status_text.push_str(value);
                }
            }
        }
    }

    fn finish_tmx_header_property(&mut self, name: &[u8]) {
        if self.tmx_header_property_name.as_deref() != Some(name) {
            return;
        }
        if let Some(key) = self.tmx_header_property_key.take() {
            if self.tmx_header_property_keep_empty || !self.tmx_header_property_value.is_empty() {
                self.metadata
                    .extensions
                    .insert(key, std::mem::take(&mut self.tmx_header_property_value));
            } else {
                self.tmx_header_property_value.clear();
            }
        }
        self.tmx_header_property_name = None;
        self.tmx_header_property_keep_empty = false;
    }

    fn finish_unit(&mut self) -> NativeResult<NativeRecord> {
        let unit = self.current.take().expect("unit exists");
        let fragment = unit
            .is_complex
            .then(|| self.read_complex_fragment())
            .transpose()?;
        let (target, targets) = match self.format {
            InterchangeFormat::Tmx => {
                if self.requested_target.is_some() {
                    (unit.target_seen.then_some(unit.target), Vec::new())
                } else {
                    (None, unit.targets)
                }
            }
            InterchangeFormat::Xliff => {
                let locale = self
                    .file_context
                    .as_ref()
                    .and_then(|context| context.target_locale.clone());
                if unit.target_seen {
                    if let Some(locale) = locale {
                        (None, vec![(locale, unit.target)])
                    } else {
                        (Some(unit.target), Vec::new())
                    }
                } else {
                    (None, Vec::new())
                }
            }
        };
        Ok(NativeRecord {
            is_complex: unit.is_complex,
            unit_id: unit.unit_id,
            source: unit.source,
            target,
            targets,
            status: unit.status,
            extensions: unit.extensions,
            fragment,
        })
    }

    fn remember_namespaces(&mut self, element: &BytesStart<'_>) -> NativeResult<()> {
        for attribute in element.attributes().with_checks(false) {
            let attribute = attribute.map_err(|error| NativeError::Xml(error.into()))?;
            let name = attribute.key.as_ref();
            if name != b"xmlns" && !name.starts_with(b"xmlns:") {
                continue;
            }
            let name = name.to_vec();
            let value = attribute.value.as_ref().to_vec();
            if let Some((_, existing)) = self
                .namespace_attributes
                .iter_mut()
                .find(|(candidate, _)| candidate == &name)
            {
                *existing = value;
            } else {
                self.namespace_attributes.push((name, value));
            }
        }
        Ok(())
    }

    fn read_complex_fragment(&mut self) -> NativeResult<Vec<u8>> {
        let length = self
            .event_end
            .checked_sub(self.current_unit_start)
            .ok_or_else(|| NativeError::Invalid("invalid complex-unit byte range".to_owned()))?;
        if length > MAX_COMPLEX_UNIT_BYTES {
            return Err(NativeError::Invalid(format!(
                "complex translation unit exceeds the {MAX_COMPLEX_UNIT_BYTES}-byte limit"
            )));
        }
        let length = usize::try_from(length).map_err(|_| {
            NativeError::Invalid("complex translation unit is too large to address".to_owned())
        })?;
        let mut unit = vec![0; length];
        self.fragment_input
            .seek(SeekFrom::Start(self.current_unit_start))?;
        self.fragment_input.read_exact(&mut unit)?;

        let namespace_bytes = self
            .namespace_attributes
            .iter()
            .map(|(name, value)| name.len() + value.len() + 4)
            .sum::<usize>();
        let mut fragment = Vec::with_capacity(unit.len() + namespace_bytes + 35);
        fragment.extend_from_slice(b"<lokit-fragment");
        for (name, value) in &self.namespace_attributes {
            fragment.push(b' ');
            fragment.extend_from_slice(name);
            fragment.extend_from_slice(b"=\"");
            fragment.extend_from_slice(value);
            fragment.push(b'"');
        }
        fragment.push(b'>');
        fragment.extend_from_slice(&unit);
        fragment.extend_from_slice(b"</lokit-fragment>");
        Ok(fragment)
    }

    fn source_locale_for_matching(&self) -> &str {
        self.requested_source
            .as_deref()
            .or(self.metadata.source_locale.as_deref())
            .unwrap_or_default()
    }
}

fn attribute_value(
    element: &BytesStart<'_>,
    wanted: &[u8],
    decoder: quick_xml::encoding::Decoder,
) -> NativeResult<Option<String>> {
    for attribute in element.attributes().with_checks(false) {
        let attribute = attribute.map_err(|error| NativeError::Xml(error.into()))?;
        if attribute.key.local_name().as_ref() == wanted {
            let value = attribute.decoded_and_normalized_value(XmlVersion::Explicit1_0, decoder)?;
            validate_xml_1_0_chars(&value, "XML attribute value")?;
            return Ok(Some(value.into_owned()));
        }
    }
    Ok(None)
}

fn validate_element_xml_chars(
    element: &BytesStart<'_>,
    decoder: quick_xml::encoding::Decoder,
) -> NativeResult<()> {
    let element_name = element.name();
    let element_name = decoder.decode(element_name.as_ref()).map_err(|error| {
        NativeError::Invalid(format!("cannot decode XML element name: {error}"))
    })?;
    validate_xml_1_0_chars(&element_name, "XML element name")?;
    for attribute in element.attributes().with_checks(false) {
        let attribute = attribute.map_err(|error| NativeError::Xml(error.into()))?;
        let attribute_name = decoder.decode(attribute.key.as_ref()).map_err(|error| {
            NativeError::Invalid(format!("cannot decode XML attribute name: {error}"))
        })?;
        validate_xml_1_0_chars(&attribute_name, "XML attribute name")?;
        let value = attribute.decoded_and_normalized_value(XmlVersion::Explicit1_0, decoder)?;
        validate_xml_1_0_chars(&value, "XML attribute value")?;
    }
    Ok(())
}

fn has_attributes_other_than(element: &BytesStart<'_>, allowed: &[&[u8]]) -> bool {
    element.attributes().with_checks(false).any(|attribute| {
        let Ok(attribute) = attribute else {
            return true;
        };
        let raw_name = attribute.key.as_ref();
        if raw_name == b"xmlns" || raw_name.starts_with(b"xmlns:") {
            return false;
        }
        let local_name = attribute.key.local_name();
        !allowed.contains(&local_name.as_ref())
    })
}

fn canonical_locale(locale: &str) -> String {
    let normalized = locale.replace('_', "-");
    let mut parts = normalized.split('-');
    let Some(language) = parts.next() else {
        return String::new();
    };
    let mut canonical = language.to_ascii_lowercase();
    for part in parts {
        canonical.push('-');
        if part.len() == 2 && part.bytes().all(|byte| byte.is_ascii_alphabetic()) {
            canonical.push_str(&part.to_ascii_uppercase());
        } else if part.len() == 4 && part.bytes().all(|byte| byte.is_ascii_alphabetic()) {
            let mut characters = part.chars();
            if let Some(first) = characters.next() {
                canonical.extend(first.to_uppercase());
            }
            canonical.push_str(&characters.as_str().to_ascii_lowercase());
        } else {
            canonical.push_str(part);
        }
    }
    canonical
}

fn base_language(locale: &str) -> String {
    locale
        .split(['-', '_'])
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase()
}

fn is_xml_whitespace(value: &str) -> bool {
    value
        .bytes()
        .all(|byte| matches!(byte, b' ' | b'\t' | b'\r' | b'\n'))
}

pub(crate) fn is_xml_1_0_chars(value: &str) -> bool {
    value.chars().all(is_xml_1_0_char)
}

fn is_xml_1_0_char(character: char) -> bool {
    matches!(character, '\u{9}' | '\u{A}' | '\u{D}')
        || ('\u{20}'..='\u{D7FF}').contains(&character)
        || ('\u{E000}'..='\u{FFFD}').contains(&character)
        || ('\u{10000}'..='\u{10FFFF}').contains(&character)
}

fn validate_xml_1_0_char(character: char, context: &str) -> NativeResult<()> {
    if is_xml_1_0_char(character) {
        return Ok(());
    }
    Err(NativeError::Invalid(format!(
        "{context} contains character U+{:04X}, which is not permitted in XML 1.0",
        u32::from(character)
    )))
}

fn validate_xml_1_0_chars(value: &str, context: &str) -> NativeResult<()> {
    if let Some(character) = value.chars().find(|character| !is_xml_1_0_char(*character)) {
        return validate_xml_1_0_char(character, context);
    }
    Ok(())
}

fn same_locale(left: &str, right: &str) -> bool {
    left.len() == right.len()
        && left.bytes().zip(right.bytes()).all(|(left, right)| {
            let left = if left == b'_' { b'-' } else { left };
            let right = if right == b'_' { b'-' } else { right };
            left.eq_ignore_ascii_case(&right)
        })
}

fn is_status_property(value: &str) -> bool {
    value == "status"
        || value == "x-status"
        || (value.starts_with("x-") && value.ends_with("-status"))
}

fn tmx_status(value: &str) -> &'static str {
    match value.to_ascii_lowercase().as_str() {
        "approved" | "signed-off" | "final" => "approved",
        "reviewed" | "review" => "reviewed",
        "translated" | "complete" => "translated",
        "new" => "new",
        "draft" | "notapproved" | "not-approved" | "unapproved" => "draft",
        "rejected" => "rejected",
        _ => UNKNOWN_STATUS,
    }
}

fn xliff_status(value: &str) -> &'static str {
    match value.to_ascii_lowercase().as_str() {
        "final" | "signed-off" => "approved",
        "translated" | "needs-review-translation" => "translated",
        "needs-review-adaptation" | "needs-review-l10n" => "reviewed",
        "new" | "needs-translation" => "new",
        _ => UNKNOWN_STATUS,
    }
}

fn xliff_v2_status(value: &str) -> &'static str {
    match value.to_ascii_lowercase().as_str() {
        "final" => "approved",
        "reviewed" => "reviewed",
        "translated" => "translated",
        "initial" => "new",
        _ => UNKNOWN_STATUS,
    }
}

fn normalize_extension_key(value: &str) -> String {
    value.to_ascii_lowercase().replace([' ', '-'], "_")
}

#[pyclass(module = "lokit._interchange_rust")]
struct Reader {
    parser: Option<NativeParser>,
    final_metadata: Metadata,
    pending_error: Option<NativeError>,
    exhausted: bool,
}

#[pymethods]
impl Reader {
    #[new]
    #[pyo3(signature = (path, format_name, source_language=None, target_language=None, mode="full"))]
    fn new(
        py: Python<'_>,
        path: &str,
        format_name: &str,
        source_language: Option<String>,
        target_language: Option<String>,
        mode: &str,
    ) -> PyResult<Self> {
        let format = InterchangeFormat::parse(format_name).map_err(native_to_py_error)?;
        let mode = ParseMode::parse(mode).map_err(native_to_py_error)?;
        let path = path.to_owned();
        let parser = py
            .detach(move || {
                NativeParser::open(
                    Path::new(&path),
                    format,
                    source_language,
                    target_language,
                    mode,
                )
            })
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
                .ok_or_else(|| PyRuntimeError::new_err("native interchange reader is closed"))?;
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
        records
            .into_iter()
            .map(|record| {
                let fragment = record
                    .fragment
                    .as_deref()
                    .map(|value| PyBytes::new(py, value));
                Ok((
                    record.is_complex,
                    record.unit_id,
                    record.source,
                    record.target,
                    record.targets,
                    record.status,
                    record.extensions,
                    fragment,
                )
                    .into_pyobject(py)?
                    .unbind())
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
    fn version(&self) -> String {
        self.metadata()
            .map_or_else(String::new, |value| value.version.clone())
    }

    #[getter]
    fn source_locale(&self) -> Option<String> {
        self.metadata()
            .and_then(|value| value.source_locale.clone())
    }

    #[getter]
    fn target_locale(&self) -> Option<String> {
        self.metadata()
            .and_then(|value| value.target_locale.clone())
    }

    #[getter]
    fn source_language(&self) -> Option<String> {
        self.metadata()
            .and_then(|value| value.source_language.clone())
    }

    #[getter]
    fn target_language(&self) -> Option<String> {
        self.metadata()
            .and_then(|value| value.target_language.clone())
    }

    #[getter]
    fn target_locales(&self) -> Vec<String> {
        self.metadata()
            .map_or_else(Vec::new, |value| value.target_locales.clone())
    }

    #[getter]
    fn target_languages(&self) -> Vec<String> {
        self.metadata()
            .map_or_else(Vec::new, |value| value.target_languages.clone())
    }

    #[getter]
    fn export_origin(&self) -> String {
        self.metadata()
            .map_or_else(String::new, |value| value.export_origin.clone())
    }

    #[getter]
    fn export_timestamp(&self) -> String {
        self.metadata()
            .map_or_else(String::new, |value| value.export_timestamp.clone())
    }

    #[getter]
    fn extensions(&self) -> HashMap<String, String> {
        self.metadata()
            .map_or_else(HashMap::new, |value| value.extensions.clone())
    }
}

impl Reader {
    fn release_parser(&mut self) {
        if let Some(parser) = self.parser.take() {
            self.final_metadata = parser.metadata;
        }
    }

    fn metadata(&self) -> Option<&Metadata> {
        Some(
            self.parser
                .as_ref()
                .map_or(&self.final_metadata, |parser| &parser.metadata),
        )
    }
}

fn native_to_py_error(error: NativeError) -> PyErr {
    match error {
        NativeError::Io(error) => PyOSError::new_err(error.to_string()),
        NativeError::Invalid(message) => PyValueError::new_err(message),
        NativeError::Xml(error) => PyValueError::new_err(format!("invalid XML: {error}")),
    }
}

#[pyfunction]
fn backend_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn _interchange_rust(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Reader>()?;
    conversion::register(module)?;
    lokit::register(module)?;
    materialize::register(module)?;
    placeholder::register(module)?;
    po::register(module)?;
    module.add_function(wrap_pyfunction!(backend_version, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::{InterchangeFormat, NativeParser, ParseMode, Reader};
    use lokit_format::id_registry::BoundedIdRegistry;

    static NEXT_FILE: AtomicUsize = AtomicUsize::new(0);

    struct TestFile(PathBuf);

    impl TestFile {
        fn new(extension: &str, contents: &str) -> Self {
            let sequence = NEXT_FILE.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "lokit-interchange-{}-{sequence}.{extension}",
                std::process::id()
            ));
            fs::write(&path, contents).expect("test input should be writable");
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TestFile {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }

    #[test]
    fn tmx_infers_canonical_locales_and_generates_stable_ids() {
        let input = TestFile::new(
            "tmx",
            r#"<?xml version="1.0"?>
<tmx version="1.4"><header srclang="*all*" creationtool="test"/>
<body>
<tu><tuv xml:lang="EN_us"><seg>Hello</seg></tuv><tuv xml:lang="fr_fr"><seg>Bonjour</seg></tuv></tu>
<tu><tuv xml:lang="EN_us"><seg>Bye</seg></tuv><tuv xml:lang="de_de"><seg>Tschuess</seg></tuv></tu>
</body></tmx>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");

        let records = parser.read_batch(8).expect("TMX units should parse");

        assert_eq!(records.len(), 2);
        assert_eq!(records[0].unit_id, "auto_0");
        assert_eq!(records[1].unit_id, "auto_1");
        assert_eq!(records[0].source, "Hello");
        assert_eq!(
            records[0].targets,
            [("fr-FR".to_owned(), "Bonjour".to_owned())]
        );
        assert_eq!(
            records[1].targets,
            [("de-DE".to_owned(), "Tschuess".to_owned())]
        );
        assert_eq!(parser.metadata.source_locale.as_deref(), Some("en-US"));
        assert_eq!(parser.metadata.target_locales, ["fr-FR", "de-DE"]);
        assert!(!parser.metadata.extensions.contains_key("xliff_version"));
    }

    #[test]
    fn duplicate_and_generated_tmx_ids_are_collision_safe() {
        let input = TestFile::new(
            "tmx",
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="alpha"><tuv xml:lang="en"><seg>one</seg></tuv></tu>
<tu tuid="alpha#2"><tuv xml:lang="en"><seg>two</seg></tuv></tu>
<tu tuid="alpha"><tuv xml:lang="en"><seg>three</seg></tuv></tu>
<tu><tuv xml:lang="en"><seg>four</seg></tuv></tu>
<tu tuid="auto_0"><tuv xml:lang="en"><seg>five</seg></tuv></tu>
</body></tmx>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");

        let records = parser.read_batch(8).expect("TMX units should parse");

        assert_eq!(
            records
                .iter()
                .map(|record| record.unit_id.as_str())
                .collect::<Vec<_>>(),
            ["alpha", "alpha#2", "alpha#3", "auto_0", "auto_0#2"]
        );
        assert_eq!(
            records
                .iter()
                .map(|record| record.extensions.get("unit_id").map(String::as_str))
                .collect::<Vec<_>>(),
            [
                Some("alpha"),
                Some("alpha#2"),
                Some("alpha"),
                Some(""),
                Some("auto_0")
            ]
        );
    }

    #[test]
    fn reader_close_preserves_resolved_ids() {
        let input = TestFile::new(
            "tmx",
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="same"><tuv xml:lang="en"><seg>one</seg></tuv></tu>
<tu tuid="same#2"><tuv xml:lang="en"><seg>two</seg></tuv></tu>
<tu tuid="same"><tuv xml:lang="en"><seg>three</seg></tuv></tu>
</body></tmx>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");
        let first_id = parser
            .current
            .as_ref()
            .expect("preamble should initialize the first unit")
            .unit_id
            .clone();
        let mut registry = BoundedIdRegistry::default();
        assert!(registry.insert(&first_id).expect("first ID should insert"));
        parser.used_unit_ids = registry;
        let records = parser.read_batch(8).expect("TMX units should parse");
        assert_eq!(
            records
                .iter()
                .map(|record| record.unit_id.as_str())
                .collect::<Vec<_>>(),
            ["same", "same#2", "same#3"]
        );
        let mut reader = Reader {
            final_metadata: parser.metadata.clone(),
            parser: Some(parser),
            pending_error: None,
            exhausted: false,
        };
        reader.close();

        assert!(reader.closed());
    }

    #[test]
    fn repeated_ids_advance_suffix_counters_at_scale() {
        let tmx_input = TestFile::new(
            "tmx",
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="alpha"><tuv xml:lang="en"><seg>one</seg></tuv></tu>
</body></tmx>"#,
        );
        let mut tmx = NativeParser::open(
            tmx_input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");
        let mut tmx_ids = BoundedIdRegistry::default();
        assert!(tmx_ids.insert("alpha").expect("first TMX ID should insert"));
        tmx.used_unit_ids = tmx_ids;
        for suffix in 2..=1_000 {
            let explicit = format!("alpha#{suffix}");
            assert_eq!(
                tmx.unique_tmx_unit_id(explicit.clone())
                    .expect("explicit suffix should resolve"),
                explicit
            );
        }
        let mut tmx_resolved = Vec::new();
        for _ in 0..2_000 {
            tmx_resolved.push(
                tmx.unique_tmx_unit_id("alpha".to_owned())
                    .expect("repeated TMX ID should resolve"),
            );
        }
        assert_eq!(tmx_resolved.first().map(String::as_str), Some("alpha#1001"));
        assert_eq!(tmx_resolved.last().map(String::as_str), Some("alpha#3000"));
        let xliff_input = TestFile::new(
            "xliff",
            r#"<xliff version="1.2"><file source-language="en" target-language="fr"><body>
<trans-unit id="same"><source>one</source><target>un</target></trans-unit>
</body></file></xliff>"#,
        );
        let mut xliff = NativeParser::open(
            xliff_input.path(),
            InterchangeFormat::Xliff,
            None,
            None,
            ParseMode::Text,
        )
        .expect("XLIFF preamble should parse");
        let mut xliff_ids = BoundedIdRegistry::default();
        assert!(xliff_ids
            .insert("same")
            .expect("first XLIFF ID should insert"));
        xliff.used_unit_ids = xliff_ids;
        assert_eq!(
            xliff
                .unique_xliff_unit_id("0:same#2".to_owned(), 0)
                .expect("explicit scoped suffix should resolve"),
            "0:same#2"
        );
        assert_eq!(
            xliff
                .unique_xliff_unit_id("0:same#4".to_owned(), 0)
                .expect("second explicit scoped suffix should resolve"),
            "0:same#4"
        );
        let mut xliff_resolved = Vec::new();
        for _ in 0..2_000 {
            xliff_resolved.push(
                xliff
                    .unique_xliff_unit_id("same".to_owned(), 0)
                    .expect("repeated XLIFF ID should resolve"),
            );
        }
        assert_eq!(&xliff_resolved[..3], ["0:same", "0:same#3", "0:same#5"]);
        assert_eq!(
            xliff_resolved.last().map(String::as_str),
            Some("0:same#2002")
        );
    }

    #[test]
    fn parse_error_is_deferred_until_after_completed_records() {
        let input = TestFile::new(
            "tmx",
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="one"><tuv xml:lang="en"><seg>one</seg></tuv></tu>
<tu tuid="two"><tuv xml:lang="en"><seg>two</seg></tuv></tu>
<tu tuid="broken"><tuv xml:lang="en"><seg>broken</tuv></tu>
</body></tmx>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");
        let first_id = parser
            .current
            .as_ref()
            .expect("preamble should initialize the first unit")
            .unit_id
            .clone();
        let mut registry = BoundedIdRegistry::default();
        assert!(registry.insert(&first_id).expect("first ID should insert"));
        parser.used_unit_ids = registry;
        let batch = parser.read_batch_preserving_prefix(8);
        assert_eq!(batch.records.len(), 2);
        assert_eq!(batch.records[0].unit_id, "one");
        assert_eq!(batch.records[1].unit_id, "two");
        assert!(batch.error.is_some());
        assert!(!batch.exhausted);
    }

    #[test]
    fn xliff_two_segments_use_native_boundaries_and_root_locales() {
        let input = TestFile::new(
            "xliff",
            r#"<xliff xmlns="urn:oasis:names:tc:xliff:document:2.0" version="2.1" srcLang="en-US" trgLang="fr-FR">
<file id="f1" original="app"><unit id="u1">
<notes><note>Keep the product name in English.</note></notes>
<segment id="s1" state="final"><source>Hello</source><target>Bonjour</target></segment>
<segment id="s2"><source>Bye <pc id="1">now</pc></source><target>Au revoir</target></segment>
</unit></file></xliff>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Xliff,
            None,
            None,
            ParseMode::Full,
        )
        .expect("XLIFF 2 preamble should parse");

        let records = parser.read_batch(8).expect("XLIFF 2 segments should parse");

        assert_eq!(parser.metadata.source_locale.as_deref(), Some("en-US"));
        assert_eq!(parser.metadata.target_locale.as_deref(), Some("fr-FR"));
        assert_eq!(records.len(), 2);
        assert_eq!(records[0].unit_id, "u1:s1");
        assert_eq!(records[0].source, "Hello");
        assert_eq!(records[0].status, "approved");
        assert!(records[0].is_complex);
        assert!(records[0].fragment.is_some());
        assert_eq!(
            records[0]
                .extensions
                .get("__lokit_native_xliff_unit_note.0")
                .map(String::as_str),
            Some("Keep the product name in English.")
        );
        assert_eq!(
            records[0].targets,
            [("fr-FR".to_owned(), "Bonjour".to_owned())]
        );
        assert_eq!(records[1].unit_id, "u1:s2");
        assert!(records[1].is_complex);
        assert_eq!(
            records[1].extensions.get("unit_id").map(String::as_str),
            Some("u1")
        );
        assert_eq!(
            records[1].extensions.get("segment_id").map(String::as_str),
            Some("s2")
        );
    }

    #[test]
    fn rejects_content_outside_or_after_the_root_element() {
        for contents in [
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="u"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>"#,
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="u"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>
</body></tmx><tmx version="1.4"></tmx>"#,
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="u"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>
</body></tmx>trailing"#,
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="u"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>
</body></tmx><?xml version="1.0"?>"#,
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="u"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>
</body></tmx><!DOCTYPE tmx>"#,
        ] {
            let input = TestFile::new("tmx", contents);
            let mut parser = NativeParser::open(
                input.path(),
                InterchangeFormat::Tmx,
                None,
                None,
                ParseMode::Full,
            )
            .expect("TMX preamble should parse");

            assert!(parser.read_batch(8).is_err());
        }
    }

    #[test]
    fn accepts_an_empty_root_and_trailing_miscellaneous_content() {
        let input = TestFile::new(
            "tmx",
            "<tmx version=\"1.4\"/>\n<!-- trailing comment --><?done?>\n",
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Full,
        )
        .expect("empty TMX should parse");

        assert!(parser.read_batch(8).expect("TMX should finish").is_empty());
    }

    #[test]
    fn rejects_invalid_xml_prologs() {
        for contents in [
            r#"<?xml version="1.0"?><?xml version="1.0"?><tmx version="1.4"/>"#,
            r#"<!DOCTYPE tmx><!DOCTYPE tmx><tmx version="1.4"/>"#,
            r#"<!-- leading comment --><?xml version="1.0"?><tmx version="1.4"/>"#,
            "\n<?xml version=\"1.0\"?><tmx version=\"1.4\"/>",
        ] {
            let input = TestFile::new("tmx", contents);

            assert!(NativeParser::open(
                input.path(),
                InterchangeFormat::Tmx,
                None,
                None,
                ParseMode::Full,
            )
            .is_err());
        }
    }

    #[test]
    fn accepts_a_complete_xml_prolog() {
        let input = TestFile::new(
            "tmx",
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!-- prolog comment --><?prepare?><!DOCTYPE tmx [<!ELEMENT tmx ANY>]>
<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="u"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>
</body></tmx>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Full,
        )
        .expect("valid XML prolog should parse");

        let records = parser.read_batch(8).expect("TMX should finish");
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].unit_id, "u");

        let bom_input = TestFile::new(
            "tmx",
            "\u{feff}<?xml version=\"1.0\" encoding=\"UTF-8\"?><tmx version=\"1.4\"/>",
        );
        let mut bom_parser = NativeParser::open(
            bom_input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Full,
        )
        .expect("UTF-8 BOM before an XML declaration should parse");
        assert!(bom_parser
            .read_batch(8)
            .expect("empty TMX should finish")
            .is_empty());
    }

    #[test]
    fn tmx_header_children_are_preserved_as_metadata() {
        let input = TestFile::new(
            "tmx",
            r#"<tmx version="1.4">
<header srclang="en"><prop type="Client Name">Acme</prop><vendor>Workbench</vendor></header>
<body><tu tuid="u1"><tuv xml:lang="en"><seg>Source</seg></tuv></tu></body>
</tmx>"#,
        );
        let parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX header metadata should parse");

        assert_eq!(
            parser
                .metadata
                .extensions
                .get("property.client_name")
                .map(String::as_str),
            Some("Acme")
        );
        assert_eq!(
            parser
                .metadata
                .extensions
                .get("property.vendor")
                .map(String::as_str),
            Some("Workbench")
        );
    }

    #[test]
    fn dtd_declarations_do_not_switch_parsers() {
        let input = TestFile::new(
            "tmx",
            r#"<!DOCTYPE tmx [<!ELEMENT tmx ANY>]>
<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="dtd"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>
</body></tmx>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("DTD declaration should parse");

        let records = parser.read_batch(8).expect("TMX unit should parse");

        assert_eq!(records.len(), 1);
        assert_eq!(records[0].unit_id, "dtd");
        assert_eq!(records[0].source, "Source");
    }

    #[test]
    fn complex_xliff_is_returned_as_a_bounded_fragment() {
        let input = TestFile::new(
            "xliff",
            r#"<?xml version="1.0"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:v="urn:vendor">
<file original="app" source-language="en-US" target-language="fr-FR" datatype="plaintext">
<body><trans-unit id="u1"><source>Hello <v:g>world</v:g>.</source><target>Bonjour</target></trans-unit></body>
</file></xliff>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Xliff,
            None,
            None,
            ParseMode::Full,
        )
        .expect("XLIFF preamble should parse");

        let records = parser.read_batch(1).expect("complex unit should parse");

        assert_eq!(records.len(), 1);
        assert!(records[0].is_complex);
        let fragment = records[0]
            .fragment
            .as_deref()
            .expect("complex unit should include its XML fragment");
        let fragment = std::str::from_utf8(fragment).expect("fragment should be UTF-8");
        assert!(fragment.starts_with("<lokit-fragment"));
        assert!(fragment.contains("xmlns:v=\"urn:vendor\""));
        assert!(fragment.contains("<v:g>world</v:g>"));
        assert!(fragment.ends_with("</lokit-fragment>"));
    }

    #[test]
    fn xml_batch_byte_budget_preserves_records_for_following_batches() {
        let input = TestFile::new(
            "tmx",
            r#"<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="u1"><tuv xml:lang="en"><seg>aaaaaaaaaaaaaaaa</seg></tuv></tu>
<tu tuid="u2"><tuv xml:lang="en"><seg>bbbbbbbbbbbbbbbb</seg></tuv></tu>
<tu tuid="u3"><tuv xml:lang="en"><seg>cccccccccccccccc</seg></tuv></tu>
</body></tmx>"#,
        );
        let mut sizing_parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");
        let first_record = sizing_parser
            .read_batch(1)
            .expect("first record should parse")
            .pop()
            .expect("first record should exist");
        let byte_budget = first_record.retained_bytes().saturating_add(1);

        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");
        let first = parser.read_batch_with_byte_budget(8, byte_budget);
        assert_eq!(first.records.len(), 1);
        assert_eq!(first.records[0].unit_id, "u1");
        assert!(first.error.is_none());
        assert!(parser.pending_record.is_some());

        let second = parser.read_batch_with_byte_budget(8, byte_budget);
        assert_eq!(second.records.len(), 1);
        assert_eq!(second.records[0].unit_id, "u2");
        assert!(second.error.is_none());

        let third = parser.read_batch_with_byte_budget(8, 1);
        assert_eq!(
            third.records.len(),
            1,
            "one oversized record must be allowed"
        );
        assert_eq!(third.records[0].unit_id, "u3");
        assert!(third.error.is_none());
        let end = parser.read_batch_with_byte_budget(8, byte_budget);
        assert!(end.records.is_empty());
        assert!(end.error.is_none());
        assert!(end.exhausted);
    }

    #[test]
    fn oversized_xml_event_buffer_is_released_after_a_batch() {
        let source = "x".repeat(super::MAX_RETAINED_EVENT_BUFFER_BYTES + super::READ_CAPACITY);
        let contents = format!(
            "<tmx version=\"1.4\"><header srclang=\"en\"/><body><tu tuid=\"large\"><tuv xml:lang=\"en\"><seg>{source}</seg></tuv></tu></body></tmx>"
        );
        let input = TestFile::new("tmx", &contents);
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");

        let records = parser.read_batch(1).expect("large record should parse");

        assert_eq!(records[0].source.len(), source.len());
        assert!(parser.buffer.capacity() <= super::MAX_RETAINED_EVENT_BUFFER_BYTES);
    }

    #[test]
    fn close_retains_metadata_discovered_during_streaming() {
        let input = TestFile::new(
            "tmx",
            r#"<tmx version="1.4"><header srclang="*all*"/><body>
<tu tuid="u1"><tuv xml:lang="en"><seg>Hello</seg></tuv><tuv xml:lang="fr"><seg>Bonjour</seg></tuv></tu>
</body></tmx>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Tmx,
            None,
            None,
            ParseMode::Text,
        )
        .expect("TMX preamble should parse");
        parser.read_batch(1).expect("TMX unit should parse");
        let mut reader = Reader {
            final_metadata: parser.metadata.clone(),
            parser: Some(parser),
            pending_error: None,
            exhausted: false,
        };

        reader.close();

        assert!(reader.closed());
        assert_eq!(reader.source_locale().as_deref(), Some("en"));
        assert_eq!(reader.target_locales(), ["fr"]);
    }

    #[test]
    fn simple_record_is_yielded_before_a_later_complex_record() {
        let input = TestFile::new(
            "xliff",
            r#"<xliff version="1.2"><file source-language="en" target-language="fr"><body>
<trans-unit id="u1"><source>Hello</source><target>Bonjour</target></trans-unit>
<trans-unit id="u2"><source>Hello <g id="1">world</g></source><target>Bonjour le monde</target></trans-unit>
</body></file></xliff>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Xliff,
            None,
            None,
            ParseMode::Full,
        )
        .expect("XLIFF preamble should parse");

        let first = parser.read_batch(1).expect("first unit should parse");
        assert_eq!(first.len(), 1);
        assert_eq!(first[0].unit_id, "u1");
        assert!(!first[0].is_complex);
        assert!(first[0].fragment.is_none());

        let second = parser.read_batch(1).expect("second unit should parse");
        assert_eq!(second.len(), 1);
        assert_eq!(second[0].unit_id, "u2");
        assert!(second[0].is_complex);
        assert!(second[0].fragment.is_some());
    }

    #[test]
    fn duplicate_xliff_ids_are_stable_across_resources() {
        let input = TestFile::new(
            "xliff",
            r#"<xliff version="1.2">
<file original="first" source-language="en" target-language="fr"><body>
<trans-unit id="same"><source>Hello</source><target>Bonjour</target></trans-unit>
</body></file>
<file original="second" source-language="en" target-language="de"><body>
<trans-unit id="same"><source>Hello <g id="1">world</g></source><target>Hallo</target></trans-unit>
</body></file></xliff>"#,
        );
        let mut parser = NativeParser::open(
            input.path(),
            InterchangeFormat::Xliff,
            None,
            None,
            ParseMode::Full,
        )
        .expect("XLIFF preamble should parse");

        let records = parser.read_batch(8).expect("XLIFF units should parse");

        assert_eq!(records.len(), 2);
        assert_eq!(records[0].unit_id, "same");
        assert_eq!(records[1].unit_id, "1:same");
        assert!(!records[0].is_complex);
        assert!(records[1].is_complex);
        assert_eq!(
            records[0].extensions.get("unit_id").map(String::as_str),
            Some("same")
        );
        assert_eq!(
            records[1].extensions.get("unit_id").map(String::as_str),
            Some("same")
        );
        assert_eq!(
            records[0].extensions.get("resource").map(String::as_str),
            Some("first")
        );
        assert_eq!(
            records[1].extensions.get("resource").map(String::as_str),
            Some("second")
        );
    }
}
