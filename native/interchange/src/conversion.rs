use std::collections::HashMap;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

use lokit_format::{Data, Meta, TargetData, TranslationStatus};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyModule};
use quick_xml::events::{BytesDecl, BytesEnd, BytesStart, BytesText, Event};
use quick_xml::Writer;

use super::{
    canonical_locale, is_xml_1_0_chars, InterchangeFormat, Metadata, NativeError, NativeParser,
    NativeRecord, NativeResult, ParseMode, DEFAULT_BATCH_SIZE, READ_CAPACITY, UNKNOWN_STATUS,
};
use crate::lokit::data_from_python;

const XLIFF_NAMESPACE: &str = "urn:oasis:names:tc:xliff:document:1.2";

#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (
    source_path,
    target_path,
    input_format,
    output_format,
    source_language=None,
    target_language=None,
    mode="full",
    copy_if_same=false,
))]
fn convert_interchange(
    py: Python<'_>,
    source_path: &str,
    target_path: &str,
    input_format: &str,
    output_format: &str,
    source_language: Option<String>,
    target_language: Option<String>,
    mode: &str,
    copy_if_same: bool,
) -> PyResult<Option<usize>> {
    let input_format = InterchangeFormat::parse(input_format).map_err(super::native_to_py_error)?;
    let output_format =
        InterchangeFormat::parse(output_format).map_err(super::native_to_py_error)?;
    let mode = ParseMode::parse(mode).map_err(super::native_to_py_error)?;
    let source_path = source_path.to_owned();
    let target_path = target_path.to_owned();
    py.detach(move || {
        convert_path(
            Path::new(&source_path),
            Path::new(&target_path),
            input_format,
            output_format,
            source_language,
            target_language,
            mode,
            copy_if_same,
        )
    })
    .map_err(super::native_to_py_error)
}

#[pyfunction]
fn export_base_interchange(
    document: &Bound<'_, PyAny>,
    target_path: &str,
    output_format: &str,
) -> PyResult<Option<usize>> {
    let output_format =
        InterchangeFormat::parse(output_format).map_err(super::native_to_py_error)?;
    let metadata = metadata_from_python(document)?;
    if metadata.source_locale.is_none()
        || metadata.target_locales.len() > 1
        || (output_format == InterchangeFormat::Xliff && metadata.target_locale.is_none())
    {
        return Ok(None);
    }

    let data = document.getattr("data")?;
    let data = data.cast::<PyDict>().map_err(|_| {
        pyo3::exceptions::PyValueError::new_err("BaseStructure.data must be a dict")
    })?;
    let xliff_data_type = if output_format == InterchangeFormat::Xliff {
        first_python_data_type(data)?
    } else {
        String::new()
    };
    if !metadata_has_valid_xml_chars(&metadata) || !is_xml_1_0_chars(&xliff_data_type) {
        return Ok(None);
    }
    let file = File::create(target_path)
        .map_err(NativeError::from)
        .map_err(super::native_to_py_error)?;
    let stream = BufWriter::with_capacity(READ_CAPACITY, file);
    let mut writer = InterchangeWriter::new(stream, output_format, &metadata, &xliff_data_type)
        .map_err(super::native_to_py_error)?;
    let mut units = 0;
    for (unit_id, value) in data.iter() {
        let unit_id: String = unit_id.extract()?;
        let data = data_from_python(&value)?;
        let Some(record) = record_from_data(unit_id, data, &metadata, output_format) else {
            return Ok(None);
        };
        if !record_has_valid_xml_chars(&record) {
            return Ok(None);
        }
        writer
            .write_record(&record, InterchangeFormat::Xliff, &metadata)
            .map_err(super::native_to_py_error)?;
        units += 1;
    }
    writer.finish().map_err(super::native_to_py_error)?;
    Ok(Some(units))
}

fn first_python_data_type(data: &Bound<'_, PyDict>) -> PyResult<String> {
    let Some((_, value)) = data.iter().next() else {
        return Ok("plaintext".to_owned());
    };
    let extensions = string_map_from_python(&value.getattr("extensions")?, "extensions")?;
    Ok(extensions
        .get("data_type")
        .cloned()
        .unwrap_or_else(|| "plaintext".to_owned()))
}

fn metadata_from_python(document: &Bound<'_, PyAny>) -> PyResult<Metadata> {
    let source_locale: String = document.getattr("source_locale")?.extract()?;
    Ok(Metadata {
        version: document.getattr("format_version")?.extract()?,
        source_locale: (!source_locale.is_empty()).then_some(source_locale),
        target_locale: document.getattr("target_locale")?.extract()?,
        source_language: document.getattr("source_language")?.extract()?,
        target_language: document.getattr("target_language")?.extract()?,
        target_locales: document.getattr("target_locales")?.extract()?,
        target_languages: document.getattr("target_languages")?.extract()?,
        export_origin: document.getattr("export_origin")?.extract()?,
        export_timestamp: document.getattr("export_timestamp")?.extract()?,
        extensions: string_map_from_python(&document.getattr("extensions")?, "extensions")?,
    })
}

fn string_map_from_python(
    value: &Bound<'_, PyAny>,
    field: &str,
) -> PyResult<HashMap<String, String>> {
    let dictionary = value
        .cast::<PyDict>()
        .map_err(|_| pyo3::exceptions::PyValueError::new_err(format!("{field} must be a dict")))?;
    dictionary
        .iter()
        .map(|(key, value)| Ok((key.extract()?, value.extract()?)))
        .collect()
}

fn record_from_data(
    unit_id: String,
    data: Data,
    metadata: &Metadata,
    output_format: InterchangeFormat,
) -> Option<NativeRecord> {
    if data.plural.is_some()
        || data.tags.is_some()
        || data.meta != Meta::default()
        || !data.comments.is_empty()
        || data.previous_context.is_some()
        || data.next_context.is_some()
        || data
            .extensions
            .iter()
            .any(|(key, _)| key.starts_with("property."))
    {
        return None;
    }
    let mut status = data.status;
    let mut selected_target: Option<(Option<String>, TranslationStatus)> = None;
    let mut sole_target: Option<(Option<String>, TranslationStatus)> = None;
    let target_count = data.targets.len();
    let mut targets = Vec::with_capacity(data.targets.len());
    for (locale, target) in data.targets {
        if !simple_target(&target) {
            return None;
        }
        if target_count == 1 {
            sole_target = Some((target.text.clone(), target.status));
        }
        if output_format == InterchangeFormat::Xliff
            && metadata
                .target_locale
                .as_deref()
                .is_some_and(|selected| locale == selected)
        {
            selected_target = Some((target.text.clone(), target.status));
        }
        if let Some(text) = target.text {
            targets.push((locale, text));
        }
    }
    let target = if output_format == InterchangeFormat::Xliff {
        let selected =
            selected_target.or_else(|| data.target.is_none().then_some(sole_target).flatten());
        if let Some((text, selected_status)) = selected {
            if selected_status != TranslationStatus::Unknown {
                status = selected_status;
            }
            targets.clear();
            text
        } else {
            targets.clear();
            data.target
        }
    } else {
        data.target
    };
    let mut extensions = data.extensions.into_iter().collect::<HashMap<_, _>>();
    extensions
        .entry("unit_id".to_owned())
        .or_insert_with(|| unit_id.clone());
    Some(NativeRecord {
        is_complex: false,
        unit_id,
        source: data.source,
        target,
        targets,
        status: status.as_str().to_owned(),
        extensions,
        fragment: None,
    })
}

fn simple_target(target: &TargetData) -> bool {
    target.tags.is_none()
        && target.plural.is_none()
        && target.meta == Meta::default()
        && target.comments.is_empty()
        && target.extensions.is_empty()
}

fn metadata_has_valid_xml_chars(metadata: &Metadata) -> bool {
    metadata
        .source_locale
        .as_deref()
        .is_none_or(is_xml_1_0_chars)
        && metadata
            .target_locale
            .as_deref()
            .is_none_or(is_xml_1_0_chars)
        && is_xml_1_0_chars(&metadata.version)
        && is_xml_1_0_chars(&metadata.export_origin)
        && is_xml_1_0_chars(&metadata.export_timestamp)
        && metadata
            .target_locales
            .iter()
            .all(|value| is_xml_1_0_chars(value))
        && metadata
            .extensions
            .iter()
            .all(|(key, value)| is_xml_1_0_chars(key) && is_xml_1_0_chars(value))
}

fn record_has_valid_xml_chars(record: &NativeRecord) -> bool {
    is_xml_1_0_chars(&record.unit_id)
        && is_xml_1_0_chars(&record.source)
        && record.target.as_deref().is_none_or(is_xml_1_0_chars)
        && record
            .targets
            .iter()
            .all(|(locale, text)| is_xml_1_0_chars(locale) && is_xml_1_0_chars(text))
        && is_xml_1_0_chars(&record.status)
        && record
            .extensions
            .iter()
            .all(|(key, value)| is_xml_1_0_chars(key) && is_xml_1_0_chars(value))
}

#[allow(clippy::too_many_arguments)]
fn convert_path(
    source_path: &Path,
    target_path: &Path,
    input_format: InterchangeFormat,
    output_format: InterchangeFormat,
    source_language: Option<String>,
    target_language: Option<String>,
    mode: ParseMode,
    copy_if_same: bool,
) -> NativeResult<Option<usize>> {
    if copy_if_same && input_format == output_format && mode == ParseMode::Full {
        let (units, metadata) = validate_document(source_path, input_format, mode)?;
        if copy_preserves_selection(
            &metadata,
            source_language.as_deref(),
            target_language.as_deref(),
        ) {
            std::fs::copy(source_path, target_path)?;
            return Ok(Some(units));
        }
    }

    let mut parser = NativeParser::open(
        source_path,
        input_format,
        source_language,
        target_language,
        mode,
    )?;
    let metadata = parser.metadata.clone();
    if !supported_metadata(&metadata, input_format, output_format) {
        return Ok(None);
    }
    let xliff_data_type = if output_format == InterchangeFormat::Xliff {
        first_native_data_type(&parser, input_format)
    } else {
        String::new()
    };
    let file = File::create(target_path)?;
    let stream = BufWriter::with_capacity(READ_CAPACITY, file);
    let mut writer = InterchangeWriter::new(stream, output_format, &metadata, &xliff_data_type)?;
    let mut units = 0;

    loop {
        let records = parser.read_batch(DEFAULT_BATCH_SIZE)?;
        if records.is_empty() {
            break;
        }
        if !supported_metadata(&parser.metadata, input_format, output_format) {
            return Ok(None);
        }
        for record in records {
            if record.is_complex {
                return Ok(None);
            }
            writer.write_record(&record, input_format, &metadata)?;
            units += 1;
        }
    }
    writer.finish()?;
    Ok(Some(units))
}

fn first_native_data_type(parser: &NativeParser, input_format: InterchangeFormat) -> String {
    if input_format != InterchangeFormat::Xliff {
        return "plaintext".to_owned();
    }
    parser
        .current
        .as_ref()
        .and_then(|unit| unit.extensions.get("data_type"))
        .cloned()
        .unwrap_or_else(|| "plaintext".to_owned())
}

fn validate_document(
    source_path: &Path,
    input_format: InterchangeFormat,
    mode: ParseMode,
) -> NativeResult<(usize, Metadata)> {
    let mut parser =
        NativeParser::open_with_xml_validation(source_path, input_format, None, None, mode, true)?;
    let mut units = 0;
    loop {
        let records = parser.read_batch(DEFAULT_BATCH_SIZE)?;
        if records.is_empty() {
            return Ok((units, parser.metadata));
        }
        units += records.len();
    }
}

fn copy_preserves_selection(
    metadata: &Metadata,
    source_language: Option<&str>,
    target_language: Option<&str>,
) -> bool {
    match (source_language, target_language) {
        (None, None) => true,
        (Some(source), Some(target)) => {
            metadata
                .source_locale
                .as_deref()
                .is_some_and(|locale| canonical_locale(locale) == canonical_locale(source))
                && metadata.target_locales.len() == 1
                && canonical_locale(&metadata.target_locales[0]) == canonical_locale(target)
        }
        _ => false,
    }
}

fn supported_metadata(
    metadata: &Metadata,
    input_format: InterchangeFormat,
    output_format: InterchangeFormat,
) -> bool {
    metadata.source_locale.is_some()
        && metadata.target_locales.len() <= 1
        && !(input_format == InterchangeFormat::Xliff
            && output_format == InterchangeFormat::Tmx
            && metadata.target_locale.is_none())
        && (output_format == InterchangeFormat::Tmx
            || input_format == InterchangeFormat::Xliff
            || metadata.target_locale.is_some())
}

enum InterchangeWriter<W: Write> {
    Tmx(Writer<W>),
    Xliff(Writer<W>),
}

impl<W: Write> InterchangeWriter<W> {
    fn new(
        stream: W,
        format: InterchangeFormat,
        metadata: &Metadata,
        xliff_data_type: &str,
    ) -> NativeResult<Self> {
        let mut writer = Writer::new(stream);
        writer.write_event(Event::Decl(BytesDecl::new("1.0", Some("UTF-8"), None)))?;
        match format {
            InterchangeFormat::Tmx => {
                write_tmx_start(&mut writer, metadata)?;
                Ok(Self::Tmx(writer))
            }
            InterchangeFormat::Xliff => {
                write_xliff_start(&mut writer, metadata, xliff_data_type)?;
                Ok(Self::Xliff(writer))
            }
        }
    }

    fn write_record(
        &mut self,
        record: &NativeRecord,
        input_format: InterchangeFormat,
        metadata: &Metadata,
    ) -> NativeResult<()> {
        match self {
            Self::Tmx(writer) => write_tmx_record(writer, record, input_format, metadata),
            Self::Xliff(writer) => write_xliff_record(writer, record, metadata),
        }
    }

    fn finish(self) -> NativeResult<()> {
        let mut writer = match self {
            Self::Tmx(mut writer) => {
                writer.write_event(Event::End(BytesEnd::new("body")))?;
                writer.write_event(Event::End(BytesEnd::new("tmx")))?;
                writer
            }
            Self::Xliff(mut writer) => {
                writer.write_event(Event::End(BytesEnd::new("body")))?;
                writer.write_event(Event::End(BytesEnd::new("file")))?;
                writer.write_event(Event::End(BytesEnd::new("xliff")))?;
                writer
            }
        };
        writer.get_mut().write_all(b"\n")?;
        writer.get_mut().flush()?;
        Ok(())
    }
}

fn write_tmx_start<W: Write>(writer: &mut Writer<W>, metadata: &Metadata) -> NativeResult<()> {
    let mut root = BytesStart::new("tmx");
    root.push_attribute(("version", "1.4"));
    writer.write_event(Event::Start(root))?;

    let source_locale = metadata
        .source_locale
        .as_deref()
        .ok_or_else(|| NativeError::Invalid("TMX export requires a source locale".to_owned()))?;
    let mut header = BytesStart::new("header");
    header.push_attribute(("creationtool", extension(metadata, "tool_name", "lokit")));
    header.push_attribute((
        "creationtoolversion",
        extension(metadata, "tool_version", "0.1"),
    ));
    header.push_attribute(("segtype", extension(metadata, "segmentation", "sentence")));
    header.push_attribute((
        "o-tmf",
        extension(metadata, "translation_memory_format", "lokit"),
    ));
    header.push_attribute((
        "adminlang",
        extension(metadata, "admin_locale", source_locale),
    ));
    header.push_attribute(("srclang", source_locale));
    header.push_attribute(("datatype", extension(metadata, "data_type", "text")));
    if !metadata.export_timestamp.is_empty() {
        header.push_attribute(("creationdate", metadata.export_timestamp.as_str()));
    }
    writer.write_event(Event::Start(header))?;

    let mut properties = metadata
        .extensions
        .iter()
        .filter_map(|(key, value)| key.strip_prefix("property.").map(|name| (name, value)))
        .collect::<Vec<_>>();
    properties.sort_unstable_by(|left, right| left.0.cmp(right.0));
    for (name, value) in properties {
        let mut property = BytesStart::new("prop");
        property.push_attribute(("type", name));
        writer.write_event(Event::Start(property))?;
        writer.write_event(Event::Text(BytesText::new(value)))?;
        writer.write_event(Event::End(BytesEnd::new("prop")))?;
    }
    writer.write_event(Event::End(BytesEnd::new("header")))?;
    writer.write_event(Event::Start(BytesStart::new("body")))?;
    Ok(())
}

fn write_xliff_start<W: Write>(
    writer: &mut Writer<W>,
    metadata: &Metadata,
    data_type: &str,
) -> NativeResult<()> {
    let source_locale = metadata
        .source_locale
        .as_deref()
        .ok_or_else(|| NativeError::Invalid("XLIFF export requires a source locale".to_owned()))?;
    let mut root = BytesStart::new("xliff");
    root.push_attribute(("xmlns", XLIFF_NAMESPACE));
    root.push_attribute(("version", "1.2"));
    writer.write_event(Event::Start(root))?;

    let mut file = BytesStart::new("file");
    file.push_attribute(("original", "lokit"));
    file.push_attribute(("datatype", data_type));
    file.push_attribute(("source-language", source_locale));
    if let Some(target_locale) = metadata.target_locale.as_deref() {
        file.push_attribute(("target-language", target_locale));
    }
    writer.write_event(Event::Start(file))?;
    writer.write_event(Event::Empty(BytesStart::new("header")))?;
    writer.write_event(Event::Start(BytesStart::new("body")))?;
    Ok(())
}

fn write_tmx_record<W: Write>(
    writer: &mut Writer<W>,
    record: &NativeRecord,
    input_format: InterchangeFormat,
    metadata: &Metadata,
) -> NativeResult<()> {
    let mut unit = BytesStart::new("tu");
    if let Some(unit_id) = output_unit_id(record) {
        unit.push_attribute(("tuid", unit_id));
    }
    writer.write_event(Event::Start(unit))?;
    if record.status != UNKNOWN_STATUS {
        let mut property = BytesStart::new("prop");
        property.push_attribute(("type", "x-status"));
        writer.write_event(Event::Start(property))?;
        writer.write_event(Event::Text(BytesText::new(&record.status)))?;
        writer.write_event(Event::End(BytesEnd::new("prop")))?;
    }

    let source_locale = metadata
        .source_locale
        .as_deref()
        .ok_or_else(|| NativeError::Invalid("TMX export requires a source locale".to_owned()))?;
    write_tuv(writer, source_locale, &record.source)?;

    if let Some(target) = record.target.as_deref() {
        let target_locale = metadata.target_locale.as_deref().ok_or_else(|| {
            NativeError::Invalid("TMX export requires a locale for the selected target".to_owned())
        })?;
        write_tuv(writer, target_locale, target)?;
    }
    for (locale, target) in &record.targets {
        if input_format == InterchangeFormat::Tmx && target.is_empty() {
            continue;
        }
        write_tuv(writer, locale, target)?;
    }
    writer.write_event(Event::End(BytesEnd::new("tu")))?;
    Ok(())
}

fn write_tuv<W: Write>(writer: &mut Writer<W>, locale: &str, text: &str) -> NativeResult<()> {
    let mut variant = BytesStart::new("tuv");
    variant.push_attribute(("xml:lang", locale));
    writer.write_event(Event::Start(variant))?;
    writer.write_event(Event::Start(BytesStart::new("seg")))?;
    if !text.is_empty() {
        writer.write_event(Event::Text(BytesText::new(text)))?;
    }
    writer.write_event(Event::End(BytesEnd::new("seg")))?;
    writer.write_event(Event::End(BytesEnd::new("tuv")))?;
    Ok(())
}

fn write_xliff_record<W: Write>(
    writer: &mut Writer<W>,
    record: &NativeRecord,
    metadata: &Metadata,
) -> NativeResult<()> {
    let mut unit = BytesStart::new("trans-unit");
    unit.push_attribute(("id", xliff_output_unit_id(record)));
    if let Some(space) = record
        .extensions
        .get("space")
        .filter(|value| !value.is_empty())
    {
        unit.push_attribute(("xml:space", space.as_str()));
    }
    writer.write_event(Event::Start(unit))?;
    write_text_element(writer, "source", &record.source, None)?;

    let target = selected_target(record, metadata)?;
    if let Some(target) = target {
        write_text_element(writer, "target", target, target_state(&record.status))?;
    }
    writer.write_event(Event::End(BytesEnd::new("trans-unit")))?;
    Ok(())
}

fn write_text_element<W: Write>(
    writer: &mut Writer<W>,
    name: &str,
    text: &str,
    state: Option<&str>,
) -> NativeResult<()> {
    let mut element = BytesStart::new(name);
    if let Some(state) = state {
        element.push_attribute(("state", state));
    }
    writer.write_event(Event::Start(element))?;
    if !text.is_empty() {
        writer.write_event(Event::Text(BytesText::new(text)))?;
    }
    writer.write_event(Event::End(BytesEnd::new(name)))?;
    Ok(())
}

fn selected_target<'a>(
    record: &'a NativeRecord,
    metadata: &Metadata,
) -> NativeResult<Option<&'a str>> {
    if let Some(target) = record.target.as_deref() {
        return Ok(Some(target));
    }
    let Some(locale) = metadata.target_locale.as_deref() else {
        return Ok(None);
    };
    let mut selected = None;
    for (target_locale, target) in &record.targets {
        if canonical_locale(target_locale) != locale {
            continue;
        }
        if selected.is_some() {
            return Err(NativeError::Invalid(format!(
                "translation unit {:?} has duplicate targets for {locale}",
                record.unit_id
            )));
        }
        selected = Some(target.as_str());
    }
    Ok(selected)
}

fn output_unit_id(record: &NativeRecord) -> Option<&str> {
    record
        .extensions
        .get("unit_id")
        .map(String::as_str)
        .filter(|value| !value.is_empty())
}

fn xliff_output_unit_id(record: &NativeRecord) -> &str {
    record
        .extensions
        .get("unit_id")
        .map_or(&record.unit_id, String::as_str)
}

fn target_state(status: &str) -> Option<&'static str> {
    match status {
        "approved" => Some("final"),
        "reviewed" => Some("needs-review-l10n"),
        "translated" => Some("translated"),
        "new" => Some("new"),
        "draft" | "rejected" => Some("needs-translation"),
        _ => None,
    }
}

fn extension<'a>(metadata: &'a Metadata, key: &str, fallback: &'a str) -> &'a str {
    metadata
        .extensions
        .get(key)
        .map_or(fallback, String::as_str)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(convert_interchange, module)?)?;
    module.add_function(wrap_pyfunction!(export_base_interchange, module)?)?;
    Ok(())
}
