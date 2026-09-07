use lokit_format::{Comment, Data, TargetData, TranslationStatus};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use crate::Metadata;

pub(crate) fn validate_fields(fields: &[String]) -> PyResult<()> {
    for field in fields {
        if !matches!(
            field.as_str(),
            "source_language"
                | "target_language"
                | "source"
                | "target"
                | "domain"
                | "unit_id"
                | "source_locale"
                | "target_locale"
                | "status"
                | "resource"
                | "project"
        ) {
            return Err(PyValueError::new_err(format!(
                "unknown dictionary field: {field}"
            )));
        }
    }
    Ok(())
}

fn matches_language(locale: &str, requested: &str) -> bool {
    if requested.is_empty() {
        return true;
    }
    let locale = locale.replace('_', "-").to_ascii_lowercase();
    let requested = requested.replace('_', "-").to_ascii_lowercase();
    if requested.contains('-') {
        locale == requested
    } else {
        crate::base_language(&locale) == requested
    }
}

fn extension<'a>(values: &'a [(String, String)], name: &str) -> Option<&'a str> {
    values
        .iter()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value.as_str())
}

fn project_name<'a>(values: &'a [(String, String)], comments: &'a [Comment]) -> Option<&'a str> {
    extension(values, "project")
        .filter(|value| !value.is_empty())
        .or_else(|| {
            comments.iter().find_map(|comment| {
                comment
                    .origin
                    .as_ref()?
                    .project
                    .as_deref()
                    .filter(|value| !value.is_empty())
            })
        })
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn project(
    py: Python<'_>,
    units: Vec<(String, Data)>,
    metadata: &Metadata,
    fields: &[String],
    source_language: &str,
    target_language: &str,
    domain: &str,
) -> PyResult<Vec<Py<PyDict>>> {
    let source_locale = metadata.source_locale.as_deref().unwrap_or("");
    let source_language = if source_language.is_empty() {
        metadata
            .source_language
            .clone()
            .unwrap_or_else(|| crate::base_language(source_locale))
    } else {
        crate::base_language(source_language)
    };
    let columns: Vec<_> = fields
        .iter()
        .map(|field| PyString::intern(py, field))
        .collect();
    let mut rows = Vec::with_capacity(units.len());
    for (key, data) in units {
        let mut row = |selected: Option<&TargetData>, locale: &str| -> PyResult<()> {
            let target = selected
                .map_or(data.target.as_deref(), |target| target.text.as_deref())
                .unwrap_or("");
            let status = selected
                .filter(|target| target.status != TranslationStatus::Unknown)
                .map_or(data.status, |target| target.status);
            let target_base = crate::base_language(if locale.is_empty() {
                target_language
            } else {
                locale
            });
            let result = PyDict::new(py);
            for (field, column) in fields.iter().zip(&columns) {
                let value = match field.as_str() {
                    "source_language" => &source_language,
                    "target_language" => &target_base,
                    "source" => &data.source,
                    "target" => target,
                    "domain" if !domain.is_empty() => domain,
                    "domain" => extension(&data.extensions, "domain")
                        .or_else(|| extension(&data.extensions, "property.domain"))
                        .unwrap_or(""),
                    "unit_id" => &key,
                    "source_locale" => source_locale,
                    "target_locale" => locale,
                    "status" => status.as_str(),
                    "resource" => extension(&data.extensions, "resource").unwrap_or(""),
                    "project" => selected
                        .and_then(|target| project_name(&target.extensions, &target.comments))
                        .or_else(|| project_name(&data.extensions, &data.comments))
                        .unwrap_or(""),
                    _ => unreachable!("validated fields"),
                };
                result.set_item(column, value)?;
            }
            rows.push(result.unbind());
            Ok(())
        };
        if data.target.is_some() {
            let locale = metadata.target_locale.as_deref().unwrap_or(target_language);
            if matches_language(locale, target_language) {
                row(None, locale)?;
            }
        }
        for (locale, target) in &data.targets {
            if !matches_language(locale, target_language)
                || (data.target.is_some() && metadata.target_locale.as_deref() == Some(locale))
            {
                continue;
            }
            row(Some(target), locale)?;
        }
        if data.target.is_none() && data.targets.is_empty() && target_language.is_empty() {
            row(None, "")?;
        }
    }
    Ok(rows)
}
