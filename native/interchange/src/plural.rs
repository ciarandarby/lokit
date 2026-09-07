use std::collections::HashMap;

use lokit_format::Plural;
use pyo3::prelude::*;

use crate::{lokit::PythonClasses, NativeError, NativeRecord, NativeResult};

pub(crate) fn finalize(
    records: &mut [NativeRecord],
    group_id: &str,
    byte_budget: usize,
) -> NativeResult<()> {
    let msgid = records
        .first()
        .map(|record| record.source.clone())
        .unwrap_or_default();
    let variant = records
        .iter()
        .skip(1)
        .find(|record| index(record) != "0")
        .map(|record| record.source.clone())
        .unwrap_or_default();
    let family = if group_id.is_empty() {
        records
            .first()
            .map(|record| {
                record
                    .unit_id
                    .rsplit_once('[')
                    .map_or(record.unit_id.as_str(), |(base, _)| base)
            })
            .unwrap_or_default()
    } else {
        group_id
    }
    .to_owned();
    let extra_per_record = msgid
        .len()
        .saturating_add(variant.len())
        .saturating_add(family.len())
        .saturating_add(512);
    let retained = records.iter().fold(0usize, |bytes, record| {
        bytes.saturating_add(record.retained_bytes())
    });
    if retained.saturating_add(extra_per_record.saturating_mul(records.len())) > byte_budget {
        return Err(NativeError::Invalid(
            "finalized XLIFF plural family exceeds its retained-data limit".to_owned(),
        ));
    }
    for record in records {
        let index = index(record).to_owned();
        record.extensions.insert("gettext_index".to_owned(), index);
        record
            .extensions
            .insert("po_msgid".to_owned(), msgid.clone());
        record
            .extensions
            .insert("po_msgid_plural".to_owned(), variant.clone());
        record
            .extensions
            .insert("po_entry_index".to_owned(), family.clone());
    }
    Ok(())
}

fn index(record: &NativeRecord) -> &str {
    record
        .extensions
        .get("unit_id")
        .unwrap_or(&record.unit_id)
        .rsplit_once('[')
        .and_then(|(_, suffix)| suffix.strip_suffix(']'))
        .filter(|value| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()))
        .unwrap_or("0")
}

pub(crate) fn from_extensions(extensions: &HashMap<String, String>) -> Option<Plural> {
    let index = extensions.get("gettext_index")?;
    let variant = extensions.get("po_msgid_plural")?;
    Some(Plural {
        variant: variant.clone(),
        extensions: vec![("gettext_index".to_owned(), index.clone())],
        ..Plural::new("")
    })
}

#[pyfunction]
fn xliff_plural(
    py: Python<'_>,
    extensions: HashMap<String, String>,
) -> PyResult<Option<Py<PyAny>>> {
    from_extensions(&extensions)
        .map(|plural| {
            PythonClasses::import(py)?
                .plural(py, plural)
                .map(Bound::unbind)
        })
        .transpose()
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(xliff_plural, module)?)?;
    Ok(())
}
