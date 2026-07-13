use std::collections::{HashMap, HashSet, VecDeque};
use std::fmt;
use std::fs::File;
use std::io::{self, BufReader};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::UNIX_EPOCH;

use pyo3::exceptions::{PyNotImplementedError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyTuple};
use quick_xml::events::{BytesStart, Event};
use quick_xml::reader::Reader as XmlReader;
use quick_xml::XmlVersion;

const READ_CAPACITY: usize = 64 * 1024;
const DEFAULT_BATCH_SIZE: usize = 256;
const MAX_BATCH_SIZE: usize = 16_384;
const ELIGIBILITY_CACHE_SIZE: usize = 128;
const UNKNOWN_STATUS: &str = "unknown";

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
    Unsupported(String),
    Io(io::Error),
    Xml(quick_xml::Error),
}

impl fmt::Display for NativeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(message) => formatter.write_str(message),
            Self::Unsupported(message) => formatter.write_str(message),
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

#[derive(Default)]
struct EligibilityState {
    root_seen: bool,
    in_unit: bool,
    in_header: bool,
    field: TextField,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
struct EligibilityKey {
    path: PathBuf,
    size: u64,
    modified_nanos: u128,
    format: InterchangeFormat,
    mode: ParseMode,
}

#[derive(Default)]
struct EligibilityCache {
    entries: HashSet<EligibilityKey>,
    order: VecDeque<EligibilityKey>,
}

impl EligibilityCache {
    fn contains(&self, key: &EligibilityKey) -> bool {
        self.entries.contains(key)
    }

    fn insert(&mut self, key: EligibilityKey) {
        if !self.entries.insert(key.clone()) {
            return;
        }
        self.order.push_back(key);
        while self.order.len() > ELIGIBILITY_CACHE_SIZE {
            if let Some(expired) = self.order.pop_front() {
                self.entries.remove(&expired);
            }
        }
    }
}

static ELIGIBILITY_CACHE: OnceLock<Mutex<EligibilityCache>> = OnceLock::new();

fn eligibility_key(
    path: &Path,
    format: InterchangeFormat,
    mode: ParseMode,
) -> NativeResult<EligibilityKey> {
    let canonical_path = path.canonicalize()?;
    let metadata = canonical_path.metadata()?;
    let modified_nanos = metadata
        .modified()
        .ok()
        .and_then(|modified| modified.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |duration| duration.as_nanos());
    Ok(EligibilityKey {
        path: canonical_path,
        size: metadata.len(),
        modified_nanos,
        format,
        mode,
    })
}

fn eligibility_is_cached(key: &EligibilityKey) -> bool {
    let cache = ELIGIBILITY_CACHE.get_or_init(|| Mutex::new(EligibilityCache::default()));
    cache
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .contains(key)
}

fn cache_eligibility(key: EligibilityKey) {
    let cache = ELIGIBILITY_CACHE.get_or_init(|| Mutex::new(EligibilityCache::default()));
    cache
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .insert(key);
}

fn check_fast_path_eligibility(
    path: &Path,
    format: InterchangeFormat,
    mode: ParseMode,
) -> NativeResult<()> {
    let key = eligibility_key(path, format, mode)?;
    if eligibility_is_cached(&key) {
        return Ok(());
    }
    let input = BufReader::with_capacity(READ_CAPACITY, File::open(path)?);
    let mut reader = XmlReader::from_reader(input);
    reader.config_mut().trim_text(false);
    let mut buffer = Vec::with_capacity(READ_CAPACITY);
    let mut state = EligibilityState::default();

    loop {
        buffer.clear();
        let event = reader.read_event_into(&mut buffer)?;
        match event {
            Event::Start(element) => {
                eligibility_start(&mut state, format, mode, &element, reader.decoder(), false)?;
            }
            Event::Empty(element) => {
                eligibility_start(&mut state, format, mode, &element, reader.decoder(), true)?;
                eligibility_end(&mut state, format, element.local_name().as_ref());
            }
            Event::End(element) => {
                eligibility_end(&mut state, format, element.local_name().as_ref());
            }
            Event::DocType(_) => {
                return Err(NativeError::Unsupported(
                    "XML documents with a DTD use the feature-complete Python parser".to_owned(),
                ));
            }
            Event::Eof => break,
            Event::Text(_)
            | Event::CData(_)
            | Event::Comment(_)
            | Event::Decl(_)
            | Event::PI(_)
            | Event::GeneralRef(_) => {}
        }
    }
    if !state.root_seen {
        return Err(NativeError::Invalid(
            "XML document does not contain the expected root element".to_owned(),
        ));
    }
    cache_eligibility(key);
    Ok(())
}

fn eligibility_start(
    state: &mut EligibilityState,
    format: InterchangeFormat,
    mode: ParseMode,
    element: &BytesStart<'_>,
    decoder: quick_xml::encoding::Decoder,
    empty: bool,
) -> NativeResult<()> {
    let local_name = element.local_name();
    let name = local_name.as_ref();
    if !state.root_seen {
        if name != format.root_name() {
            return Err(NativeError::Invalid(format!(
                "expected {} XML root, found {}",
                String::from_utf8_lossy(format.root_name()).to_uppercase(),
                String::from_utf8_lossy(name)
            )));
        }
        let default_version = if format == InterchangeFormat::Tmx {
            "1.4"
        } else {
            "1.2"
        };
        let version = attribute_value(element, b"version", decoder)?
            .unwrap_or_else(|| default_version.to_owned());
        if format == InterchangeFormat::Xliff && !version.starts_with('1') {
            return Err(NativeError::Unsupported(format!(
                "XLIFF {version} is not supported by the native reader"
            )));
        }
        if format == InterchangeFormat::Tmx && !version.starts_with("1.4") {
            return Err(NativeError::Unsupported(format!(
                "TMX {version} uses the feature-complete Python parser"
            )));
        }
        state.root_seen = true;
        return Ok(());
    }

    if !state.in_unit && name == format.unit_name() {
        if mode == ParseMode::Full {
            let unsupported_attributes = match format {
                InterchangeFormat::Tmx => has_attributes_other_than(element, &[b"tuid".as_slice()]),
                InterchangeFormat::Xliff => {
                    has_attributes_other_than(element, &[b"id".as_slice(), b"space".as_slice()])
                }
            };
            if unsupported_attributes {
                return unsupported_fast_path("translation-unit metadata attributes");
            }
        }
        state.in_unit = !empty;
        return Ok(());
    }

    if !state.in_unit {
        if format == InterchangeFormat::Tmx {
            if name == b"header" {
                state.in_header = !empty;
            } else if state.in_header {
                return unsupported_fast_path("TMX header child elements");
            }
        }
        return Ok(());
    }

    if state.field != TextField::None {
        return unsupported_fast_path("inline XML content");
    }
    match format {
        InterchangeFormat::Tmx => match name {
            b"tuv" => {}
            b"seg" => {
                if !empty {
                    state.field = TextField::Source;
                }
            }
            b"prop" | b"note" if mode == ParseMode::Full => {
                return unsupported_fast_path("TMX unit metadata");
            }
            b"prop" | b"note" => {}
            _ => return unsupported_fast_path("unsupported TMX unit structure"),
        },
        InterchangeFormat::Xliff => match name {
            b"source" => {
                if !empty {
                    state.field = TextField::Source;
                }
            }
            b"target" => {
                if !empty {
                    state.field = TextField::Target;
                }
            }
            b"note" if mode == ParseMode::Full => {
                return unsupported_fast_path("XLIFF unit notes");
            }
            b"note" => {}
            _ => return unsupported_fast_path("unsupported XLIFF unit structure"),
        },
    }
    Ok(())
}

fn eligibility_end(state: &mut EligibilityState, format: InterchangeFormat, name: &[u8]) {
    if name == format.unit_name() {
        state.in_unit = false;
        state.field = TextField::None;
    } else if format == InterchangeFormat::Tmx && name == b"header" {
        state.in_header = false;
    } else if name == b"seg" || name == b"source" || name == b"target" {
        state.field = TextField::None;
    }
}

fn unsupported_fast_path<T>(feature: &str) -> NativeResult<T> {
    Err(NativeError::Unsupported(format!(
        "{feature} uses the feature-complete Python parser"
    )))
}

struct NativeParser {
    xml: XmlReader<BufReader<File>>,
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
    current: Option<UnitBuilder>,
    root_seen: bool,
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
        check_fast_path_eligibility(path, format, mode)?;
        let file = File::open(path)?;
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
            current: None,
            root_seen: false,
            eof: false,
        };
        parser.prepare_preamble()?;
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
        let mut records = Vec::with_capacity(batch_size.min(DEFAULT_BATCH_SIZE));
        while records.len() < batch_size {
            if self.eof {
                break;
            }
            if let Some(record) = self.process_next_event()? {
                records.push(record);
            }
        }
        Ok(records)
    }

    fn process_next_event(&mut self) -> NativeResult<Option<NativeRecord>> {
        let mut buffer = std::mem::take(&mut self.buffer);
        buffer.clear();
        let event = self.xml.read_event_into(&mut buffer)?;
        let result = self.process_event(event);
        self.buffer = buffer;
        result
    }

    fn process_event(&mut self, event: Event<'_>) -> NativeResult<Option<NativeRecord>> {
        match event {
            Event::Start(element) => self.process_start(&element),
            Event::Empty(element) => self.process_empty(&element),
            Event::End(element) => {
                let local_name = element.local_name();
                let name = local_name.as_ref();
                if self.current.is_some() && name == self.format.unit_name() {
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
                self.process_text(&value);
                Ok(None)
            }
            Event::CData(text) => {
                let value = text.decode().map_err(|error| {
                    NativeError::Invalid(format!("cannot decode XML CDATA: {error}"))
                })?;
                self.process_text(&value);
                Ok(None)
            }
            Event::GeneralRef(reference) => {
                let value = if let Some(character) =
                    reference.resolve_char_ref().map_err(|error| {
                        NativeError::Invalid(format!("invalid XML character reference: {error}"))
                    })? {
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
                self.eof = true;
                Ok(None)
            }
            Event::Comment(_) | Event::Decl(_) | Event::PI(_) | Event::DocType(_) => Ok(None),
        }
    }

    fn process_start(&mut self, element: &BytesStart<'_>) -> NativeResult<Option<NativeRecord>> {
        let local_name = element.local_name();
        let name = local_name.as_ref();
        if !self.root_seen {
            self.initialize_root(element, name)?;
            return Ok(None);
        }
        if self.current.is_none() && name == self.format.unit_name() {
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
        let local_name = element.local_name();
        let name = local_name.as_ref();
        if !self.root_seen {
            self.initialize_root(element, name)?;
            self.eof = true;
            return Ok(None);
        }
        if self.current.is_none() && name == self.format.unit_name() {
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
        if self.format == InterchangeFormat::Xliff && !version.starts_with('1') {
            return Err(NativeError::Unsupported(format!(
                "XLIFF {version} is not supported by the native reader"
            )));
        }
        self.metadata.version = version.clone();
        if self.format == InterchangeFormat::Xliff {
            self.metadata
                .extensions
                .insert("xliff_version".to_owned(), version);
        }
        Ok(())
    }

    fn process_preamble_start(
        &mut self,
        element: &BytesStart<'_>,
        name: &[u8],
    ) -> NativeResult<()> {
        match self.format {
            InterchangeFormat::Tmx if name == b"header" => self.initialize_tmx_header(element),
            InterchangeFormat::Xliff if name == b"file" => self.initialize_xliff_file(element),
            _ => Ok(()),
        }
    }

    fn process_preamble_empty(
        &mut self,
        element: &BytesStart<'_>,
        name: &[u8],
    ) -> NativeResult<()> {
        self.process_preamble_start(element, name)
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
            .or_else(|| self.requested_target.clone());
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

    fn begin_unit(&mut self, element: &BytesStart<'_>) -> NativeResult<()> {
        let (unit_id, extensions, has_unhandled_attributes) = match self.format {
            InterchangeFormat::Tmx => {
                let raw_id =
                    attribute_value(element, b"tuid", self.xml.decoder())?.unwrap_or_default();
                let unit_id = if raw_id.is_empty() {
                    let generated = format!("auto_{}", self.generated_unit_index);
                    self.generated_unit_index += 1;
                    generated
                } else {
                    raw_id
                };
                (
                    unit_id,
                    HashMap::new(),
                    has_attributes_other_than(element, &[b"tuid".as_slice()]),
                )
            }
            InterchangeFormat::Xliff => {
                let context = self.file_context.clone().unwrap_or_default();
                let raw_id =
                    attribute_value(element, b"id", self.xml.decoder())?.unwrap_or_default();
                let unit_id = if raw_id.is_empty() {
                    context.index.to_string()
                } else {
                    raw_id.clone()
                };
                let mut extensions = HashMap::from([
                    ("resource".to_owned(), context.original),
                    ("resource_index".to_owned(), context.index.to_string()),
                    ("unit_id".to_owned(), raw_id),
                ]);
                if !context.data_type.is_empty() {
                    extensions.insert("data_type".to_owned(), context.data_type);
                }
                if let Some(space) = attribute_value(element, b"space", self.xml.decoder())? {
                    extensions.insert("space".to_owned(), space);
                }
                (
                    unit_id,
                    extensions,
                    has_attributes_other_than(element, &[b"id".as_slice(), b"space".as_slice()]),
                )
            }
        };
        let mut unit = UnitBuilder::new(unit_id, extensions);
        if self.mode == ParseMode::Full && has_unhandled_attributes {
            unit.is_complex = true;
        }
        self.current = Some(unit);
        Ok(())
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
        let requested_target = self.requested_target.clone();
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
                let source_locale = self.source_locale_for_matching().to_owned();
                let unit = self.current.as_mut().expect("unit exists");
                let locale = unit.tmx_tuv_locale.as_deref().unwrap_or_default();
                unit.field = if same_locale(locale, &source_locale) {
                    unit.source_seen = true;
                    TextField::Source
                } else if requested_target
                    .as_deref()
                    .is_none_or(|target| same_locale(locale, target))
                {
                    unit.target_seen = true;
                    TextField::Target
                } else {
                    TextField::None
                };
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
                if self.mode.includes_status() {
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
        let source_locale = self.source_locale_for_matching().to_owned();
        let requested_target = self.requested_target.clone();
        let Some(unit) = self.current.as_mut() else {
            if self.format == InterchangeFormat::Xliff && name == b"file" {
                self.file_context = None;
            }
            return;
        };
        match self.format {
            InterchangeFormat::Tmx => match name {
                b"seg" => unit.field = TextField::None,
                b"tuv" => {
                    unit.field = TextField::None;
                    if requested_target.is_none() {
                        if let Some(locale) = unit.tmx_tuv_locale.as_deref() {
                            if !same_locale(locale, &source_locale) {
                                unit.targets
                                    .push((locale.to_owned(), std::mem::take(&mut unit.target)));
                            }
                        }
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

    fn finish_unit(&mut self) -> NativeResult<NativeRecord> {
        let unit = self.current.take().expect("unit exists");
        if unit.is_complex {
            return Err(NativeError::Unsupported(
                "complex translation unit escaped native eligibility scan".to_owned(),
            ));
        }
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
            fragment: None,
        })
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
            return Ok(Some(value.into_owned()));
        }
    }
    Ok(None)
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

fn same_locale(left: &str, right: &str) -> bool {
    left.replace('_', "-")
        .eq_ignore_ascii_case(&right.replace('_', "-"))
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

#[pyclass(module = "lokit._interchange_rust")]
struct Reader {
    parser: Option<NativeParser>,
    final_metadata: Metadata,
}

#[pymethods]
impl Reader {
    #[new]
    #[pyo3(signature = (path, format_name, source_language=None, target_language=None, mode="full"))]
    fn new(
        path: &str,
        format_name: &str,
        source_language: Option<String>,
        target_language: Option<String>,
        mode: &str,
    ) -> PyResult<Self> {
        let format = InterchangeFormat::parse(format_name).map_err(native_to_py_error)?;
        let mode = ParseMode::parse(mode).map_err(native_to_py_error)?;
        match NativeParser::open(
            Path::new(path),
            format,
            source_language,
            target_language,
            mode,
        ) {
            Ok(parser) => Ok(Self {
                final_metadata: parser.metadata.clone(),
                parser: Some(parser),
            }),
            Err(NativeError::Unsupported(message)) => Err(PyNotImplementedError::new_err(message)),
            Err(error) => Err(native_to_py_error(error)),
        }
    }

    #[pyo3(signature = (batch_size=DEFAULT_BATCH_SIZE))]
    fn read_batch(&mut self, py: Python<'_>, batch_size: usize) -> PyResult<Vec<Py<PyTuple>>> {
        if batch_size == 0 || batch_size > MAX_BATCH_SIZE {
            return Err(PyValueError::new_err(format!(
                "batch_size must be between 1 and {MAX_BATCH_SIZE}"
            )));
        }
        let parser = self
            .parser
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("native interchange reader is closed"))?;
        let records = py
            .detach(|| parser.read_batch(batch_size))
            .map_err(native_to_py_error)?;
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
        if let Some(parser) = self.parser.take() {
            self.final_metadata = parser.metadata;
        }
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
        NativeError::Unsupported(message) => PyNotImplementedError::new_err(message),
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
    module.add_function(wrap_pyfunction!(backend_version, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::{InterchangeFormat, NativeError, NativeParser, ParseMode, Reader};

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
    fn complex_xliff_is_rejected_before_any_records_are_yielded() {
        let input = TestFile::new(
            "xliff",
            r#"<?xml version="1.0"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:v="urn:vendor">
<file original="app" source-language="en-US" target-language="fr-FR" datatype="plaintext">
<body><trans-unit id="u1"><source>Hello <v:g>world</v:g>.</source><target>Bonjour</target></trans-unit></body>
</file></xliff>"#,
        );
        let result = NativeParser::open(
            input.path(),
            InterchangeFormat::Xliff,
            None,
            None,
            ParseMode::Full,
        );

        match result {
            Err(NativeError::Unsupported(message)) => {
                assert!(message.contains("inline XML content"));
            }
            Err(error) => panic!("unexpected eligibility error: {error}"),
            Ok(_) => panic!("complex XLIFF must fall back before native streaming"),
        }
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
        };

        reader.close();

        assert!(reader.closed());
        assert_eq!(reader.source_locale().as_deref(), Some("en"));
        assert_eq!(reader.target_locales(), ["fr"]);
    }

    #[test]
    fn eligibility_cache_invalidates_when_file_changes() {
        let input = TestFile::new(
            "xliff",
            r#"<xliff version="1.2"><file source-language="en" target-language="fr"><body>
<trans-unit id="u1"><source>Hello</source><target>Bonjour</target></trans-unit>
</body></file></xliff>"#,
        );
        NativeParser::open(
            input.path(),
            InterchangeFormat::Xliff,
            None,
            None,
            ParseMode::Full,
        )
        .expect("simple XLIFF should populate the eligibility cache");
        fs::write(
            input.path(),
            r#"<xliff version="1.2"><file source-language="en" target-language="fr"><body>
<trans-unit id="u1"><source>Hello <g id="1">world</g></source><target>Bonjour</target></trans-unit>
</body></file></xliff>"#,
        )
        .expect("changed test input should be writable");

        let result = NativeParser::open(
            input.path(),
            InterchangeFormat::Xliff,
            None,
            None,
            ParseMode::Full,
        );

        assert!(matches!(result, Err(NativeError::Unsupported(_))));
    }
}
