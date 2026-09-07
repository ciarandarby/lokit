use std::fs::File;
use std::io::{BufRead, BufReader};
use std::str::FromStr;

use lokit_format::id_registry::BoundedIdRegistry;
use lokit_format::{Comment, Data, TargetData, TranslationStatus};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule};

use crate::lokit::PythonClasses;

const MAX_ROW_BYTES: usize = 64 * 1024 * 1024;

struct Records<R> {
    input: R,
    parser: csv_core::Reader,
}

impl<R: BufRead> Records<R> {
    fn next(&mut self, limit: usize) -> crate::NativeResult<Option<Vec<String>>> {
        let mut text = Vec::new();
        let mut ends = Vec::new();
        let mut raw_bytes = 0usize;
        let mut output = [0u8; 8192];
        let mut fields = [0usize; 256];
        loop {
            let (result, consumed, written, field_count) =
                self.parser
                    .read_record(self.input.fill_buf()?, &mut output, &mut fields);
            self.input.consume(consumed);
            raw_bytes = raw_bytes.saturating_add(consumed);
            let retained = text
                .len()
                .saturating_add(written)
                .saturating_mul(3)
                .saturating_add(ends.len().saturating_add(field_count).saturating_mul(56));
            if raw_bytes > limit || retained > limit {
                return Err(crate::NativeError::Invalid(
                    "CSV row exceeds the 64 MiB safety limit".into(),
                ));
            }
            text.extend_from_slice(&output[..written]);
            ends.extend_from_slice(&fields[..field_count]);
            match result {
                csv_core::ReadRecordResult::Record => {
                    let text = std::str::from_utf8(&text).map_err(|error| {
                        crate::NativeError::Invalid(format!(
                            "CSV row contains invalid UTF-8: {error}"
                        ))
                    })?;
                    let mut start = 0;
                    return Ok(Some(
                        ends.into_iter()
                            .map(|end| {
                                let field = text[start..end].to_owned();
                                start = end;
                                field
                            })
                            .collect(),
                    ));
                }
                csv_core::ReadRecordResult::End => return Ok(None),
                _ => {}
            }
        }
    }
}

struct Layout {
    source: isize,
    targets: Vec<(String, isize)>,
    id: isize,
    status: isize,
    comment: isize,
    extras: Vec<(String, isize)>,
}

impl Layout {
    fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        fn columns(value: &Bound<'_, PyAny>, name: &str) -> PyResult<Vec<(String, isize)>> {
            value
                .getattr(name)?
                .call_method0("items")?
                .try_iter()?
                .map(|item| item?.extract())
                .collect()
        }
        Ok(Self {
            source: value.getattr("source_column")?.extract()?,
            targets: columns(value, "target_columns")?,
            id: value.getattr("id_column")?.extract()?,
            status: value.getattr("status_column")?.extract()?,
            comment: value.getattr("comment_column")?.extract()?,
            extras: columns(value, "extra_columns")?,
        })
    }

    fn data(&self, row: &[String], target_locale: Option<&str>) -> Data {
        let mut data = Data::new(cell(row, self.source));
        data.status =
            TranslationStatus::from_str(&cell(row, self.status).trim().to_ascii_lowercase())
                .unwrap_or_default();
        for (locale, index) in &self.targets {
            let text = cell(row, *index);
            if target_locale == Some(locale.as_str()) {
                data.target = (!text.is_empty()).then(|| text.to_owned());
            } else if target_locale.is_none() && !locale.is_empty() {
                data.targets.push((
                    locale.clone(),
                    TargetData {
                        text: (!text.is_empty()).then(|| text.to_owned()),
                        status: data.status,
                        ..TargetData::default()
                    },
                ));
            }
        }
        let comment = cell(row, self.comment).trim();
        if !comment.is_empty() {
            data.comments.push(Comment::new(comment));
        }
        for (name, index) in &self.extras {
            let text = cell(row, *index);
            if !text.is_empty() {
                data.extensions.push((name.clone(), text.to_owned()));
            }
        }
        data
    }
}

fn cell(row: &[String], index: isize) -> &str {
    usize::try_from(index)
        .ok()
        .and_then(|index| row.get(index))
        .map_or("", String::as_str)
}

#[pyclass(module = "lokit._interchange_rust")]
struct CsvReader {
    reader: Option<Records<BufReader<File>>>,
    first: Option<Vec<String>>,
    layout: Option<Layout>,
    ids: BoundedIdRegistry,
    index: usize,
    pending_error: Option<String>,
}

#[pymethods]
impl CsvReader {
    #[new]
    fn new(py: Python<'_>, path: String) -> PyResult<Self> {
        py.detach(move || {
            let mut reader = Records {
                input: BufReader::with_capacity(crate::READ_CAPACITY, File::open(path)?),
                parser: csv_core::Reader::new(),
            };
            let first = reader
                .next(MAX_ROW_BYTES)
                .map_err(crate::native_to_py_error)?;
            Ok(Self {
                reader: Some(reader),
                first,
                layout: None,
                ids: BoundedIdRegistry::default(),
                index: 0,
                pending_error: None,
            })
        })
    }

    #[getter]
    fn first_row(&self) -> Option<Vec<String>> {
        self.first.clone()
    }

    #[getter]
    fn closed(&self) -> bool {
        self.reader.is_none()
    }

    fn configure(&mut self, layout: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.layout.is_some() {
            return Err(PyValueError::new_err("CSV reader is already configured"));
        }
        self.layout = Some(Layout::from_python(layout)?);
        if layout.getattr("has_header")?.extract::<bool>()?
            && !layout
                .getattr("include_header_as_data")?
                .extract::<bool>()?
        {
            self.first = None;
        }
        Ok(())
    }

    #[pyo3(signature = (target_locale=None, runtime_placeholders=false, inline_placeholders=false, syntaxes=None, batch_size=256))]
    fn read_batch(
        &mut self,
        py: Python<'_>,
        target_locale: Option<String>,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        syntaxes: Option<Vec<String>>,
        batch_size: usize,
    ) -> PyResult<Vec<(String, Py<PyAny>)>> {
        let options = crate::placeholder::projection_options(
            runtime_placeholders,
            inline_placeholders,
            syntaxes,
        )?;
        let rows = py.detach(|| self.rows(batch_size))?;
        let layout = self
            .layout
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("CSV reader requires a layout"))?;
        let (units, error) = py.detach(|| {
            let mut units = Vec::with_capacity(rows.len());
            for (key, row) in rows {
                match crate::placeholder::project_native(
                    layout.data(&row, target_locale.as_deref()),
                    &options,
                ) {
                    Ok(data) => units.push((key, data)),
                    Err(error) => return (units, Some(error)),
                }
            }
            (units, None)
        });
        if let Some(error) = error {
            self.close();
            if units.is_empty() {
                return Err(error);
            }
            self.pending_error = Some(error.to_string());
        }
        let classes = PythonClasses::import(py)?;
        units
            .into_iter()
            .map(|(key, data)| classes.data(py, data).map(|data| (key, data.unbind())))
            .collect()
    }

    #[pyo3(signature = (batch_size=256))]
    fn read_target_batch(
        &mut self,
        py: Python<'_>,
        batch_size: usize,
    ) -> PyResult<Vec<Py<PyDict>>> {
        let rows = py.detach(|| self.rows(batch_size))?;
        let layout = self
            .layout
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("CSV reader requires a layout"))?;
        let classes = PythonClasses::import(py)?;
        let mut result = Vec::with_capacity(rows.len());
        for (key, row) in rows {
            let targets = PyDict::new(py);
            for (locale, _) in &layout.targets {
                targets.set_item(
                    locale,
                    (&key, classes.data(py, layout.data(&row, Some(locale)))?),
                )?;
            }
            result.push(targets.unbind());
        }
        Ok(result)
    }

    fn close(&mut self) {
        self.reader = None;
        self.first = None;
        self.ids = BoundedIdRegistry::default();
    }
}

impl CsvReader {
    fn rows(&mut self, batch_size: usize) -> PyResult<Vec<(String, Vec<String>)>> {
        if batch_size == 0 || batch_size > crate::MAX_BATCH_SIZE {
            return Err(PyValueError::new_err(
                "batch_size must be between 1 and 16384",
            ));
        }
        if let Some(error) = self.pending_error.take() {
            return Err(PyValueError::new_err(error));
        }
        let layout = self
            .layout
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("CSV reader requires a layout"))?;
        let mut rows = Vec::with_capacity(batch_size.min(crate::DEFAULT_BATCH_SIZE));
        let mut bytes = 0usize;
        while rows.len() < batch_size && bytes < crate::MAX_BATCH_BYTES {
            let row = if let Some(row) = self.first.take() {
                row
            } else {
                let Some(reader) = self.reader.as_mut() else {
                    break;
                };
                match reader.next(MAX_ROW_BYTES) {
                    Ok(Some(row)) => row,
                    Ok(None) => {
                        self.reader = None;
                        break;
                    }
                    Err(error) => {
                        self.pending_error = Some(error.to_string());
                        self.reader = None;
                        break;
                    }
                }
            };
            let size = row
                .iter()
                .fold(0usize, |size, value| size.saturating_add(value.len() + 24));
            if size > MAX_ROW_BYTES {
                self.pending_error = Some("CSV row exceeds 64 MiB".into());
                self.reader = None;
                break;
            }
            let raw = cell(&row, layout.id);
            let preferred = if raw.is_empty() {
                format!("csv:{}", self.index)
            } else {
                raw.to_owned()
            };
            let key = crate::identity::resolve_id(&mut self.ids, &preferred)?;
            self.index += 1;
            bytes = bytes.saturating_add(size);
            rows.push((key, row));
        }
        if rows.is_empty() {
            if let Some(error) = self.pending_error.take() {
                return Err(PyValueError::new_err(error));
            }
        }
        Ok(rows)
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<CsvReader>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn fields_are_bounded_before_string_allocation() {
        let mut reader = Records {
            input: Cursor::new(",".repeat(10_000).into_bytes()),
            parser: csv_core::Reader::new(),
        };
        assert!(reader.next(1024).is_err());
        assert!(reader.input.position() <= 256);
    }

    #[test]
    fn quoted_data_is_bounded_while_accumulating() {
        let payload = format!("\"{}\"", "x".repeat(100_000));
        let mut reader = Records {
            input: Cursor::new(payload.into_bytes()),
            parser: csv_core::Reader::new(),
        };
        assert!(reader.next(1024).is_err());
        assert!(reader.input.position() <= 8193);
    }
}
