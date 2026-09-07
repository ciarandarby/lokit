use std::collections::HashMap;
use std::path::Path;
use std::str::FromStr;

use lokit_format::{BaseStructure, Data, TargetData, TranslationStatus};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::{PyDict, PyModule};

use super::{
    native_to_py_error, InterchangeFormat, Metadata, NativeParser, NativeRecord, ParseMode,
    DEFAULT_BATCH_SIZE,
};
use crate::lokit::PythonClasses;

fn document_header(metadata: Metadata) -> BaseStructure {
    BaseStructure {
        source_locale: metadata.source_locale.unwrap_or_default(),
        target_locale: metadata.target_locale,
        data: Vec::new(),
        target_locales: metadata.target_locales,
        format_version: "0.1".to_owned(),
        export_origin: metadata.export_origin,
        export_timestamp: metadata.export_timestamp,
        source_language: metadata.source_language,
        target_language: metadata.target_language,
        target_languages: metadata.target_languages,
        extensions: metadata.extensions.into_iter().collect(),
    }
}

pub(crate) fn materialize_record(
    record: NativeRecord,
    format: InterchangeFormat,
    domain: Option<&str>,
) -> (String, Data) {
    let NativeRecord {
        unit_id,
        source,
        target,
        targets,
        status,
        mut extensions,
        data: semantic_data,
        ..
    } = record;
    extensions
        .entry("unit_id".to_owned())
        .or_insert_with(|| unit_id.clone());
    if let Some(domain) = domain.filter(|value| !value.is_empty()) {
        extensions.insert("domain".to_owned(), domain.to_owned());
    }
    extensions.retain(|key, _| !key.starts_with(super::XLIFF_UNIT_NOTE_PREFIX));
    if let Some(data) = semantic_data {
        let mut data = *data;
        for (key, value) in extensions {
            if let Some((_, previous)) = data
                .extensions
                .iter_mut()
                .find(|(candidate, _)| candidate == &key)
            {
                *previous = value;
            } else {
                data.extensions.push((key, value));
            }
        }
        data.plural = crate::plural::from_extensions(&data.extensions.iter().cloned().collect())
            .or(data.plural);
        return (unit_id, data);
    }
    let status = TranslationStatus::from_str(&status).unwrap_or_default();
    let mut data = Data::new(source);
    data.status = status;
    data.plural = crate::plural::from_extensions(&extensions);
    data.extensions = extensions.into_iter().collect();
    match format {
        InterchangeFormat::Tmx => {
            data.target = target;
            data.targets = targets
                .into_iter()
                .map(|(locale, text)| {
                    (
                        locale,
                        TargetData {
                            text: (!text.is_empty()).then_some(text),
                            ..TargetData::default()
                        },
                    )
                })
                .collect();
        }
        InterchangeFormat::Xliff => {
            data.target = target;
            data.targets = targets
                .into_iter()
                .map(|(locale, text)| {
                    (
                        locale,
                        TargetData {
                            text: Some(text),
                            status,
                            ..TargetData::default()
                        },
                    )
                })
                .collect();
        }
    }
    (unit_id, data)
}

#[pyfunction]
#[pyo3(signature = (
    path,
    format_name,
    source_language=None,
    target_language=None,
    domain=None,
    mode="full",
    runtime_placeholders=false,
    inline_placeholders=false,
    syntaxes=None,
))]
#[allow(clippy::too_many_arguments)]
fn materialize_interchange(
    py: Python<'_>,
    path: &str,
    format_name: &str,
    source_language: Option<String>,
    target_language: Option<String>,
    domain: Option<String>,
    mode: &str,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    syntaxes: Option<Vec<String>>,
) -> PyResult<Option<Py<PyAny>>> {
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
    materialize_parser(
        py,
        parser,
        format,
        domain,
        runtime_placeholders,
        inline_placeholders,
        syntaxes,
    )
}

#[pyfunction]
#[pyo3(signature = (data, format_name, source_language=None, target_language=None, domain=None, mode="full", runtime_placeholders=true, inline_placeholders=true, syntaxes=None))]
#[allow(clippy::too_many_arguments)]
fn materialize_interchange_bytes(
    py: Python<'_>,
    data: PyBackedBytes,
    format_name: &str,
    source_language: Option<String>,
    target_language: Option<String>,
    domain: Option<String>,
    mode: &str,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    syntaxes: Option<Vec<String>>,
) -> PyResult<Py<PyAny>> {
    let format = InterchangeFormat::parse(format_name).map_err(native_to_py_error)?;
    let mode = ParseMode::parse(mode).map_err(native_to_py_error)?;
    let parser = py
        .detach(move || {
            NativeParser::open_input(
                crate::input::Input::bytes(data),
                format,
                source_language,
                target_language,
                mode,
                false,
            )
        })
        .map_err(native_to_py_error)?;
    materialize_parser(
        py,
        parser,
        format,
        domain,
        runtime_placeholders,
        inline_placeholders,
        syntaxes,
    )?
    .ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(
            "native materialization did not produce a document",
        )
    })
}

#[allow(clippy::too_many_arguments)]
fn materialize_parser(
    py: Python<'_>,
    mut parser: NativeParser,
    format: InterchangeFormat,
    domain: Option<String>,
    runtime_placeholders: bool,
    inline_placeholders: bool,
    syntaxes: Option<Vec<String>>,
) -> PyResult<Option<Py<PyAny>>> {
    let classes = PythonClasses::import(py)?;
    parser.emit_fragments = false;
    let options = crate::placeholder::projection_options(
        runtime_placeholders,
        inline_placeholders,
        syntaxes,
    )?;
    let data = PyDict::new(py);
    let mut identities: HashMap<(String, String, String), Vec<String>> = HashMap::new();
    let mut indexed = false;
    loop {
        let records = py
            .detach(|| parser.read_batch(DEFAULT_BATCH_SIZE))
            .map_err(native_to_py_error)?;
        if records.is_empty() {
            break;
        }
        if format == InterchangeFormat::Xliff
            && parser.metadata.target_locales.len() > 1
            && !indexed
        {
            for (key, value) in data.iter() {
                if !value.getattr("target")?.is_none() {
                    continue;
                }
                let extensions = value.getattr("extensions")?;
                let extensions = extensions.cast::<PyDict>()?;
                let field = |name: &str| -> PyResult<String> {
                    extensions
                        .get_item(name)?
                        .map(|value| value.extract())
                        .transpose()
                        .map(Option::unwrap_or_default)
                };
                let raw_id = field("unit_id")?;
                if raw_id.is_empty() {
                    continue;
                }
                let identity = (
                    field("resource")?,
                    format!("{raw_id}:{}", field("segment_id")?),
                    value.getattr("source")?.extract()?,
                );
                identities.entry(identity).or_default().push(key.extract()?);
            }
            indexed = true;
        }
        for record in records {
            let (unit_id, unit) = materialize_record(record, format, domain.as_deref());
            let unit = crate::placeholder::project_native(unit, &options)?;
            if indexed && unit.target.is_none() {
                let ext = |key: &str| {
                    unit.extensions
                        .iter()
                        .find(|(candidate, _)| candidate == key)
                        .map(|(_, value)| value.as_str())
                        .unwrap_or("")
                };
                if !ext("unit_id").is_empty() {
                    let identity = (
                        ext("resource").to_owned(),
                        format!("{}:{}", ext("unit_id"), ext("segment_id")),
                        unit.source.clone(),
                    );
                    let candidates = identities.entry(identity).or_default();
                    let mut merged = false;
                    for key in candidates.iter() {
                        let existing = data.get_item(key)?.expect("registered unit");
                        let targets = existing.getattr("targets")?;
                        let targets = targets.cast::<PyDict>()?;
                        if (!targets.is_empty() || !unit.targets.is_empty())
                            && unit
                                .targets
                                .iter()
                                .all(|(locale, _)| !targets.contains(locale).unwrap_or(true))
                        {
                            let incoming = classes.data(py, unit.clone())?;
                            targets.update(
                                incoming.getattr("targets")?.cast::<PyDict>()?.as_mapping(),
                            )?;
                            existing.getattr("extensions")?.cast::<PyDict>()?.update(
                                incoming
                                    .getattr("extensions")?
                                    .cast::<PyDict>()?
                                    .as_mapping(),
                            )?;
                            merged = true;
                            break;
                        }
                    }
                    if merged {
                        continue;
                    }
                    candidates.push(unit_id.clone());
                }
            }
            data.set_item(unit_id, classes.data(py, unit)?)?;
        }
    }
    if format == InterchangeFormat::Xliff && parser.metadata.target_locales.len() == 1 {
        collapse_python_targets(py, &data)?;
    }
    classes
        .base_structure_with_data(py, document_header(parser.metadata), data)
        .map(Bound::unbind)
        .map(Some)
}

fn collapse_python_targets(py: Python<'_>, data: &Bound<'_, PyDict>) -> PyResult<()> {
    for (_, unit) in data.iter() {
        let targets = unit.getattr("targets")?;
        let targets = targets.cast::<PyDict>()?;
        if targets.len() != 1 {
            continue;
        }
        let (_, target) = targets.iter().next().expect("one target");
        unit.setattr("target", target.getattr("text")?)?;
        unit.setattr("status", target.getattr("status")?)?;
        let tags = unit.getattr("tags")?;
        let target_tags = target.getattr("tags")?;
        if !tags.is_none() && !target_tags.is_none() {
            tags.setattr("target_tag_map", target_tags.getattr("tag_map")?)?;
            tags.setattr("target_parts", target_tags.getattr("parts")?)?;
        }
        unit.setattr("targets", PyDict::new(py))?;
    }
    Ok(())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(materialize_interchange, module)?)?;
    module.add_function(wrap_pyfunction!(materialize_interchange_bytes, module)?)?;
    Ok(())
}
