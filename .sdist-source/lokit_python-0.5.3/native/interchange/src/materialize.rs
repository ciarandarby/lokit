use std::path::Path;
use std::str::FromStr;

use lokit_format::{BaseStructure, Data, TargetData, TranslationStatus};
use pyo3::prelude::*;
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

fn materialize_record(
    record: NativeRecord,
    format: InterchangeFormat,
    domain: Option<&str>,
) -> Option<(String, Data)> {
    let NativeRecord {
        unit_id,
        source,
        target,
        targets,
        status,
        mut extensions,
        ..
    } = record;
    extensions
        .entry("unit_id".to_owned())
        .or_insert_with(|| unit_id.clone());
    if let Some(domain) = domain.filter(|value| !value.is_empty()) {
        extensions.insert("domain".to_owned(), domain.to_owned());
    }
    let status = TranslationStatus::from_str(&status).unwrap_or_default();
    let mut data = Data::new(source);
    data.status = status;
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
            if targets.len() > 1 {
                return None;
            }
            data.target = targets.into_iter().next().map(|(_, text)| text).or(target);
        }
    }
    Some((unit_id, data))
}

#[pyfunction]
#[pyo3(signature = (
    path,
    format_name,
    source_language=None,
    target_language=None,
    domain=None,
    mode="full",
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
) -> PyResult<Option<Py<PyAny>>> {
    let format = InterchangeFormat::parse(format_name).map_err(native_to_py_error)?;
    let mode = ParseMode::parse(mode).map_err(native_to_py_error)?;
    let path = path.to_owned();
    let mut parser = py
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
    let classes = PythonClasses::import(py)?;
    let data = PyDict::new(py);
    loop {
        let records = py
            .detach(|| parser.read_batch(DEFAULT_BATCH_SIZE))
            .map_err(native_to_py_error)?;
        if records.is_empty() {
            break;
        }
        if records.iter().any(|record| record.is_complex)
            || (format == InterchangeFormat::Xliff && parser.metadata.target_locales.len() > 1)
        {
            return Ok(None);
        }
        for record in records {
            let Some((unit_id, unit)) = materialize_record(record, format, domain.as_deref())
            else {
                return Ok(None);
            };
            data.set_item(unit_id, classes.data(py, unit)?)?;
        }
    }
    if format == InterchangeFormat::Xliff && parser.metadata.target_locales.len() > 1 {
        return Ok(None);
    }
    classes
        .base_structure_with_data(py, document_header(parser.metadata), data)
        .map(Bound::unbind)
        .map(Some)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(materialize_interchange, module)?)?;
    Ok(())
}
