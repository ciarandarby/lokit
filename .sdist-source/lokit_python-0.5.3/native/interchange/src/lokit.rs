use std::fs::File;
use std::io::{BufReader, BufWriter};
use std::path::{Path, PathBuf};
use std::str::FromStr;

use lokit_format::{
    AdjacentContext, BaseStructure, CanonicalWriter, CodePart, Comment, Data, ErrorCode, Meta,
    Origin, ParseError, Plural, PluralCategory, SegmentPart, StreamingReader, Tags, TargetData,
    TargetTags, TextPart, TieData, TieType, TranslationStatus, WriteError,
};
use pyo3::exceptions::{
    PyBlockingIOError, PyBrokenPipeError, PyConnectionAbortedError, PyConnectionRefusedError,
    PyConnectionResetError, PyFileExistsError, PyFileNotFoundError, PyInterruptedError,
    PyIsADirectoryError, PyMemoryError, PyNotADirectoryError, PyOSError, PyPermissionError,
    PyRuntimeError, PyTimeoutError, PyValueError,
};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyDictMethods, PyList, PyModule, PyTuple};

const READ_CAPACITY: usize = 64 * 1024;
const DEFAULT_BATCH_SIZE: usize = 256;
const MAX_BATCH_SIZE: usize = 16_384;

type NativeReader = StreamingReader<BufReader<File>>;
type NativeWriter = CanonicalWriter<BufWriter<File>>;

#[derive(Debug)]
enum ReaderFailure {
    Io {
        error: std::io::Error,
        filename: String,
    },
    Parse(ParseError),
}

#[pyclass(module = "lokit._interchange_rust")]
pub(crate) struct LokitReader {
    reader: Option<NativeReader>,
    metadata: BaseStructure,
    pending_error: Option<ReaderFailure>,
}

#[pymethods]
impl LokitReader {
    #[new]
    fn new(py: Python<'_>, path: &str) -> PyResult<Self> {
        let path = path.to_owned();
        let reader = py
            .detach(move || open_reader(Path::new(&path)))
            .map_err(reader_to_py_error)?;
        let metadata = reader.document_header().clone();
        Ok(Self {
            reader: Some(reader),
            metadata,
            pending_error: None,
        })
    }

    #[pyo3(signature = (batch_size=DEFAULT_BATCH_SIZE))]
    fn read_batch(&mut self, py: Python<'_>, batch_size: usize) -> PyResult<Vec<Py<PyTuple>>> {
        validate_batch_size(batch_size).map_err(PyValueError::new_err)?;
        if let Some(error) = self.pending_error.take() {
            self.reader = None;
            return Err(reader_to_py_error(error));
        }
        let reader = self
            .reader
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("Lokit reader is closed"))?;
        let batch = py.detach(|| read_batch_preserving_prefix(reader, batch_size));
        if let Some(error) = batch.error {
            if batch.records.is_empty() {
                self.reader = None;
                return Err(reader_to_py_error(error));
            }
            self.pending_error = Some(error);
        }
        let classes = PythonClasses::import(py)?;
        batch
            .records
            .into_iter()
            .map(|(unit_id, data)| {
                let unit_id = unit_id.into_pyobject(py)?.into_any();
                let data = classes.data(py, data)?;
                Ok(PyTuple::new(py, [unit_id, data])?.unbind())
            })
            .collect()
    }

    #[pyo3(signature = (locale, legacy_locale=None, include_missing=true, batch_size=DEFAULT_BATCH_SIZE))]
    fn read_target_batch(
        &mut self,
        py: Python<'_>,
        locale: &str,
        legacy_locale: Option<&str>,
        include_missing: bool,
        batch_size: usize,
    ) -> PyResult<Vec<Py<PyTuple>>> {
        validate_batch_size(batch_size).map_err(PyValueError::new_err)?;
        if let Some(error) = self.pending_error.take() {
            self.reader = None;
            return Err(reader_to_py_error(error));
        }
        let reader = self
            .reader
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("Lokit reader is closed"))?;
        let locale = locale.to_owned();
        let legacy_locale = legacy_locale.map(str::to_owned);
        let batch = py.detach(|| {
            read_target_batch_preserving_prefix(
                reader,
                batch_size,
                &locale,
                legacy_locale.as_deref(),
                include_missing,
            )
        });
        if let Some(error) = batch.error {
            if batch.records.is_empty() {
                self.reader = None;
                return Err(reader_to_py_error(error));
            }
            self.pending_error = Some(error);
        }
        let classes = PythonClasses::import(py)?;
        batch
            .records
            .into_iter()
            .map(|(unit_id, data)| {
                let unit_id = unit_id.into_pyobject(py)?.into_any();
                let data = classes.data(py, data)?;
                Ok(PyTuple::new(py, [unit_id, data])?.unbind())
            })
            .collect()
    }

    fn close(&mut self) {
        self.reader = None;
        self.pending_error = None;
    }

    #[getter]
    fn closed(&self) -> bool {
        self.reader.is_none()
    }

    #[getter]
    fn source_locale(&self) -> String {
        self.metadata.source_locale.clone()
    }

    #[getter]
    fn target_locale(&self) -> Option<String> {
        self.metadata.target_locale.clone()
    }

    #[getter]
    fn target_locales(&self) -> Vec<String> {
        self.metadata.target_locales.clone()
    }

    #[getter]
    fn format_version(&self) -> String {
        self.metadata.format_version.clone()
    }

    #[getter]
    fn export_origin(&self) -> String {
        self.metadata.export_origin.clone()
    }

    #[getter]
    fn export_timestamp(&self) -> String {
        self.metadata.export_timestamp.clone()
    }

    #[getter]
    fn source_language(&self) -> Option<String> {
        self.metadata.source_language.clone()
    }

    #[getter]
    fn target_language(&self) -> Option<String> {
        self.metadata.target_language.clone()
    }

    #[getter]
    fn target_languages(&self) -> Vec<String> {
        self.metadata.target_languages.clone()
    }

    #[getter]
    fn extensions(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        Ok(string_map_to_python(py, &self.metadata.extensions)?.unbind())
    }
}

fn validate_batch_size(batch_size: usize) -> Result<(), String> {
    if batch_size == 0 || batch_size > MAX_BATCH_SIZE {
        Err(format!("batch_size must be between 1 and {MAX_BATCH_SIZE}"))
    } else {
        Ok(())
    }
}

fn open_reader(path: &Path) -> Result<NativeReader, ReaderFailure> {
    let file = File::open(path).map_err(|error| ReaderFailure::Io {
        error,
        filename: path.to_string_lossy().into_owned(),
    })?;
    StreamingReader::new(BufReader::with_capacity(READ_CAPACITY, file))
        .map_err(ReaderFailure::Parse)
}

#[cfg(test)]
fn read_batch(
    reader: &mut NativeReader,
    batch_size: usize,
) -> Result<Vec<(String, Data)>, ReaderFailure> {
    let batch = read_batch_preserving_prefix(reader, batch_size);
    match batch.error {
        Some(error) => Err(error),
        None => Ok(batch.records),
    }
}

struct PrefixBatch {
    records: Vec<(String, Data)>,
    error: Option<ReaderFailure>,
}

fn read_batch_preserving_prefix(reader: &mut NativeReader, batch_size: usize) -> PrefixBatch {
    let mut records = Vec::with_capacity(batch_size.min(DEFAULT_BATCH_SIZE));
    while records.len() < batch_size {
        match reader.next_unit() {
            Ok(Some(record)) => records.push(record),
            Ok(None) => break,
            Err(error) => {
                return PrefixBatch {
                    records,
                    error: Some(ReaderFailure::Parse(error)),
                };
            }
        }
    }
    PrefixBatch {
        records,
        error: None,
    }
}

fn read_target_batch_preserving_prefix(
    reader: &mut NativeReader,
    batch_size: usize,
    locale: &str,
    legacy_locale: Option<&str>,
    include_missing: bool,
) -> PrefixBatch {
    let mut records = Vec::with_capacity(batch_size.min(DEFAULT_BATCH_SIZE));
    while records.len() < batch_size {
        match reader.next_unit() {
            Ok(Some((unit_id, data))) => {
                if let Some(selected) =
                    select_target_data(data, locale, legacy_locale, include_missing)
                {
                    records.push((unit_id, selected));
                }
            }
            Ok(None) => break,
            Err(error) => {
                return PrefixBatch {
                    records,
                    error: Some(ReaderFailure::Parse(error)),
                };
            }
        }
    }
    PrefixBatch {
        records,
        error: None,
    }
}

fn select_target_data(
    mut data: Data,
    locale: &str,
    legacy_locale: Option<&str>,
    include_missing: bool,
) -> Option<Data> {
    if let Some(index) = data
        .targets
        .iter()
        .position(|(candidate, _)| candidate == locale)
    {
        let (_, selected) = data.targets.remove(index);
        data.targets.clear();
        data.target = selected.text;
        if selected.status != TranslationStatus::Unknown {
            data.status = selected.status;
        }
        if selected.plural.is_some() {
            data.plural = selected.plural;
        }
        merge_meta(&mut data.meta, selected.meta);
        if !selected.comments.is_empty() {
            data.comments = selected.comments;
        }
        merge_string_map(&mut data.extensions, selected.extensions);
        set_target_tags(&mut data, selected.tags);
        return Some(data);
    }

    data.targets.clear();
    if data.target.is_some() && legacy_locale == Some(locale) {
        return Some(data);
    }
    data.target = None;
    set_target_tags(&mut data, None);
    include_missing.then_some(data)
}

fn merge_meta(base: &mut Meta, target: Meta) {
    if target.usage_count.is_some() {
        base.usage_count = target.usage_count;
    }
    if target.last_used.is_some() {
        base.last_used = target.last_used;
    }
    if target.first_used.is_some() {
        base.first_used = target.first_used;
    }
    if target.created.is_some() {
        base.created = target.created;
    }
    if target.updated.is_some() {
        base.updated = target.updated;
    }
    if target.max_length.is_some() {
        base.max_length = target.max_length;
    }
    if target.min_length.is_some() {
        base.min_length = target.min_length;
    }
    merge_string_map(&mut base.extensions, target.extensions);
}

fn merge_string_map(values: &mut Vec<(String, String)>, additions: Vec<(String, String)>) {
    for (key, value) in additions {
        if let Some((_, existing)) = values.iter_mut().find(|(candidate, _)| candidate == &key) {
            *existing = value;
        } else {
            values.push((key, value));
        }
    }
}

fn set_target_tags(data: &mut Data, target: Option<TargetTags>) {
    match (&mut data.tags, target) {
        (Some(tags), Some(target_tags)) => {
            tags.target_tag_map = target_tags.tag_map;
            tags.target_parts = target_tags.parts;
        }
        (Some(tags), None) => {
            tags.target_tag_map.clear();
            tags.target_parts.clear();
        }
        (slot @ None, Some(target_tags)) => {
            *slot = Some(Tags {
                source_tag_map: Vec::new(),
                target_tag_map: target_tags.tag_map,
                source_parts: Vec::new(),
                target_parts: target_tags.parts,
            });
        }
        (None, None) => {}
    }
}

#[pyclass(module = "lokit._interchange_rust")]
pub(crate) struct LokitWriter {
    writer: Option<NativeWriter>,
    path: PathBuf,
    completed: bool,
    aborted: bool,
}

#[pymethods]
impl LokitWriter {
    #[new]
    #[pyo3(signature = (
        path,
        source_locale,
        target_locale,
        target_locales,
        format_version,
        export_origin,
        export_timestamp,
        source_language,
        target_language,
        target_languages,
        extensions,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        path: &str,
        source_locale: String,
        target_locale: Option<String>,
        target_locales: Vec<String>,
        format_version: String,
        export_origin: String,
        export_timestamp: String,
        source_language: Option<String>,
        target_language: Option<String>,
        target_languages: Vec<String>,
        extensions: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let path = PathBuf::from(path);
        let header = BaseStructure {
            source_locale,
            target_locale,
            data: Vec::new(),
            target_locales,
            format_version,
            export_origin,
            export_timestamp,
            source_language,
            target_language,
            target_languages,
            extensions: string_map_from_python(extensions, "extensions")?,
        };
        let result = py.detach({
            let path = path.clone();
            move || open_writer(&path, &header)
        });
        match result {
            Ok(writer) => Ok(Self {
                writer: Some(writer),
                path,
                completed: false,
                aborted: false,
            }),
            Err(error) => {
                remove_partial_file(&path);
                Err(write_to_py_error(error))
            }
        }
    }

    fn write(&mut self, py: Python<'_>, unit_id: &str, data: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.writer.is_none() {
            return Err(PyRuntimeError::new_err(format!(
                "Lokit writer is {}",
                self.state_name()
            )));
        }
        let data = match data_from_python(data) {
            Ok(data) => data,
            Err(error) => {
                self.abort_internal();
                return Err(error);
            }
        };
        let Some(writer) = self.writer.as_mut() else {
            return Err(PyRuntimeError::new_err("Lokit writer is closed"));
        };
        let result = py.detach(|| writer.write_unit(unit_id, &data));
        if let Err(error) = result {
            self.abort_internal();
            return Err(write_to_py_error(error));
        }
        Ok(())
    }

    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        let Some(mut writer) = self.writer.take() else {
            return Ok(());
        };
        match py.detach(move || writer.finish()) {
            Ok(()) => {
                self.completed = true;
                Ok(())
            }
            Err(error) => {
                self.aborted = true;
                remove_partial_file(&self.path);
                Err(write_to_py_error(error))
            }
        }
    }

    fn abort(&mut self, py: Python<'_>) -> PyResult<()> {
        if self.writer.is_none() {
            return Ok(());
        }
        self.writer = None;
        self.aborted = true;
        let path = self.path.clone();
        py.detach(move || remove_partial_file_result(&path))
            .map_err(io_to_py_error)
    }

    #[getter]
    fn closed(&self) -> bool {
        self.writer.is_none()
    }
}

impl LokitWriter {
    fn abort_internal(&mut self) {
        self.writer = None;
        self.aborted = true;
        remove_partial_file(&self.path);
    }

    const fn state_name(&self) -> &'static str {
        if self.completed {
            "closed"
        } else if self.aborted {
            "aborted"
        } else {
            "closed"
        }
    }
}

impl Drop for LokitWriter {
    fn drop(&mut self) {
        if self.writer.is_some() {
            self.abort_internal();
        }
    }
}

fn open_writer(path: &Path, header: &BaseStructure) -> Result<NativeWriter, WriteError> {
    let file = File::create(path).map_err(WriteError::from)?;
    let mut writer = CanonicalWriter::new(BufWriter::with_capacity(READ_CAPACITY, file));
    writer.start(header)?;
    Ok(writer)
}

pub(crate) struct PythonClasses<'py> {
    adjacent_context: Bound<'py, PyAny>,
    base_structure: Bound<'py, PyAny>,
    code_part: Bound<'py, PyAny>,
    comment: Bound<'py, PyAny>,
    data: Bound<'py, PyAny>,
    meta: Bound<'py, PyAny>,
    origin: Bound<'py, PyAny>,
    plural: Bound<'py, PyAny>,
    plural_category: Bound<'py, PyAny>,
    status_approved: Bound<'py, PyAny>,
    status_draft: Bound<'py, PyAny>,
    status_new: Bound<'py, PyAny>,
    status_rejected: Bound<'py, PyAny>,
    status_reviewed: Bound<'py, PyAny>,
    status_translated: Bound<'py, PyAny>,
    status_unknown: Bound<'py, PyAny>,
    tags: Bound<'py, PyAny>,
    target_data: Bound<'py, PyAny>,
    target_tags: Bound<'py, PyAny>,
    text_part: Bound<'py, PyAny>,
    tie_data: Bound<'py, PyAny>,
    tie_type: Bound<'py, PyAny>,
}

impl<'py> PythonClasses<'py> {
    pub(crate) fn import(py: Python<'py>) -> PyResult<Self> {
        let structure = py.import("lokit.data.structure")?;
        let tag_types = py.import("lokit.data.tag_types")?;
        let translation_status = structure.getattr("TranslationStatus")?;
        Ok(Self {
            adjacent_context: structure.getattr("AdjacentContext")?,
            base_structure: structure.getattr("BaseStructure")?,
            code_part: structure.getattr("CodePart")?,
            comment: structure.getattr("Comment")?,
            data: structure.getattr("Data")?,
            meta: structure.getattr("Meta")?,
            origin: structure.getattr("Origin")?,
            plural: structure.getattr("Plural")?,
            plural_category: structure.getattr("PluralCategory")?,
            status_approved: translation_status.getattr("APPROVED")?,
            status_draft: translation_status.getattr("DRAFT")?,
            status_new: translation_status.getattr("NEW")?,
            status_rejected: translation_status.getattr("REJECTED")?,
            status_reviewed: translation_status.getattr("REVIEWED")?,
            status_translated: translation_status.getattr("TRANSLATED")?,
            status_unknown: translation_status.getattr("UNKNOWN")?,
            tags: structure.getattr("Tags")?,
            target_data: structure.getattr("TargetData")?,
            target_tags: structure.getattr("TargetTags")?,
            text_part: structure.getattr("TextPart")?,
            tie_data: tag_types.getattr("TieData")?,
            tie_type: tag_types.getattr("TieType")?,
        })
    }

    pub(crate) fn base_structure_with_data(
        &self,
        py: Python<'py>,
        structure: BaseStructure,
        data: Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.base_structure.call1((
            structure.source_locale,
            structure.target_locale,
            data,
            PyTuple::new(py, structure.target_locales)?,
            structure.format_version,
            structure.export_origin,
            structure.export_timestamp,
            structure.source_language,
            structure.target_language,
            PyTuple::new(py, structure.target_languages)?,
            string_map_to_python(py, &structure.extensions)?,
        ))
    }

    pub(crate) fn data(&self, py: Python<'py>, data: Data) -> PyResult<Bound<'py, PyAny>> {
        let targets = PyDict::new(py);
        for (locale, target) in data.targets {
            targets.set_item(locale, self.target_data(py, target)?)?;
        }
        let plural = data
            .plural
            .map(|value| self.plural(py, value))
            .transpose()?;
        let tags = data.tags.map(|value| self.tags(py, value)).transpose()?;
        let comments = self.comments(py, data.comments)?;
        let previous_context = data
            .previous_context
            .map(|value| self.adjacent_context(py, value))
            .transpose()?;
        let next_context = data
            .next_context
            .map(|value| self.adjacent_context(py, value))
            .transpose()?;
        self.data.call1((
            data.source,
            data.target,
            targets,
            plural,
            tags,
            self.meta(py, data.meta)?,
            self.translation_status(data.status),
            comments,
            previous_context,
            next_context,
            string_map_to_python(py, &data.extensions)?,
        ))
    }

    fn target_data(&self, py: Python<'py>, target: TargetData) -> PyResult<Bound<'py, PyAny>> {
        let tags = target
            .tags
            .map(|value| self.target_tags(py, value))
            .transpose()?;
        let plural = target
            .plural
            .map(|value| self.plural(py, value))
            .transpose()?;
        self.target_data.call1((
            target.text,
            self.translation_status(target.status),
            tags,
            plural,
            self.meta(py, target.meta)?,
            self.comments(py, target.comments)?,
            string_map_to_python(py, &target.extensions)?,
        ))
    }

    fn plural(&self, py: Python<'py>, plural: Plural) -> PyResult<Bound<'py, PyAny>> {
        let category = plural
            .category
            .map(|value| self.plural_category.call1((value.as_str(),)))
            .transpose()?;
        self.plural.call1((
            plural.variant,
            plural.count,
            category,
            string_map_to_python(py, &plural.extensions)?,
        ))
    }

    fn meta(&self, py: Python<'py>, meta: Meta) -> PyResult<Bound<'py, PyAny>> {
        self.meta.call1((
            meta.usage_count,
            meta.last_used,
            meta.first_used,
            meta.created,
            meta.updated,
            meta.max_length,
            meta.min_length,
            string_map_to_python(py, &meta.extensions)?,
        ))
    }

    fn origin(&self, py: Python<'py>, origin: Origin) -> PyResult<Bound<'py, PyAny>> {
        self.origin.call1((
            origin.system,
            origin.project,
            origin.creator_id,
            string_map_to_python(py, &origin.extensions)?,
        ))
    }

    fn comments(&self, py: Python<'py>, comments: Vec<Comment>) -> PyResult<Bound<'py, PyList>> {
        let result = PyList::empty(py);
        for comment in comments {
            let origin = comment
                .origin
                .map(|value| self.origin(py, value))
                .transpose()?;
            result.append(self.comment.call1((
                comment.context,
                comment.timestamp,
                origin,
                comment.context_key,
                string_map_to_python(py, &comment.extensions)?,
            ))?)?;
        }
        Ok(result)
    }

    fn adjacent_context(
        &self,
        py: Python<'py>,
        context: AdjacentContext,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.adjacent_context.call1((
            context.unit_id,
            context.source,
            context.target,
            string_map_to_python(py, &context.extensions)?,
        ))
    }

    fn tags(&self, py: Python<'py>, tags: Tags) -> PyResult<Bound<'py, PyAny>> {
        self.tags.call1((
            self.tie_map(py, tags.source_tag_map)?,
            self.tie_map(py, tags.target_tag_map)?,
            self.parts(py, tags.source_parts)?,
            self.parts(py, tags.target_parts)?,
        ))
    }

    fn target_tags(&self, py: Python<'py>, tags: TargetTags) -> PyResult<Bound<'py, PyAny>> {
        self.target_tags
            .call1((self.tie_map(py, tags.tag_map)?, self.parts(py, tags.parts)?))
    }

    pub(crate) fn tie_map(
        &self,
        py: Python<'py>,
        values: Vec<(String, TieData)>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let result = PyDict::new(py);
        for (key, value) in values {
            result.set_item(key, self.tie_data(py, value)?)?;
        }
        Ok(result)
    }

    fn tie_data(&self, py: Python<'py>, tie: TieData) -> PyResult<Bound<'py, PyAny>> {
        self.tie_data.call1((
            tie.id,
            self.tie_type.call1((tie.r#type.as_str(),))?,
            string_map_to_python(py, &tie.attributes)?,
            tie.attribute_data,
            tie.position,
            tie.order,
            tie.pair_id,
            tie.original_name,
            tie.original_text,
        ))
    }

    pub(crate) fn parts(
        &self,
        py: Python<'py>,
        parts: Vec<SegmentPart>,
    ) -> PyResult<Bound<'py, PyList>> {
        let result = PyList::empty(py);
        for part in parts {
            match part {
                SegmentPart::Text(TextPart { value }) => {
                    result.append(self.text_part.call1((value,))?)?;
                }
                SegmentPart::Code(CodePart { r#ref }) => {
                    result.append(self.code_part.call1((r#ref,))?)?;
                }
            }
        }
        Ok(result)
    }

    fn translation_status(&self, value: TranslationStatus) -> Bound<'py, PyAny> {
        match value {
            TranslationStatus::New => self.status_new.clone(),
            TranslationStatus::Draft => self.status_draft.clone(),
            TranslationStatus::Translated => self.status_translated.clone(),
            TranslationStatus::Reviewed => self.status_reviewed.clone(),
            TranslationStatus::Approved => self.status_approved.clone(),
            TranslationStatus::Rejected => self.status_rejected.clone(),
            TranslationStatus::Unknown => self.status_unknown.clone(),
        }
    }
}

fn string_map_to_python<'py>(
    py: Python<'py>,
    values: &[(String, String)],
) -> PyResult<Bound<'py, PyDict>> {
    let result = PyDict::new(py);
    for (key, value) in values {
        result.set_item(key, value)?;
    }
    Ok(result)
}

pub(crate) fn data_from_python(value: &Bound<'_, PyAny>) -> PyResult<Data> {
    Ok(Data {
        source: value.getattr("source")?.extract()?,
        target: value.getattr("target")?.extract()?,
        targets: targets_from_python(&value.getattr("targets")?)?,
        plural: optional_attribute(value, "plural")?
            .map(|item| plural_from_python(&item))
            .transpose()?,
        tags: optional_attribute(value, "tags")?
            .map(|item| tags_from_python(&item))
            .transpose()?,
        meta: meta_from_python(&value.getattr("meta")?)?,
        status: translation_status_from_python(&value.getattr("status")?)?,
        comments: comments_from_python(&value.getattr("comments")?)?,
        previous_context: optional_attribute(value, "previous_context")?
            .map(|item| adjacent_context_from_python(&item))
            .transpose()?,
        next_context: optional_attribute(value, "next_context")?
            .map(|item| adjacent_context_from_python(&item))
            .transpose()?,
        extensions: string_map_from_python(&value.getattr("extensions")?, "extensions")?,
    })
}

fn target_data_from_python(value: &Bound<'_, PyAny>) -> PyResult<TargetData> {
    Ok(TargetData {
        text: value.getattr("text")?.extract()?,
        status: translation_status_from_python(&value.getattr("status")?)?,
        tags: optional_attribute(value, "tags")?
            .map(|item| target_tags_from_python(&item))
            .transpose()?,
        plural: optional_attribute(value, "plural")?
            .map(|item| plural_from_python(&item))
            .transpose()?,
        meta: meta_from_python(&value.getattr("meta")?)?,
        comments: comments_from_python(&value.getattr("comments")?)?,
        extensions: string_map_from_python(&value.getattr("extensions")?, "extensions")?,
    })
}

fn plural_from_python(value: &Bound<'_, PyAny>) -> PyResult<Plural> {
    let category = optional_attribute(value, "category")?
        .map(|item| enum_from_python::<PluralCategory>(&item, "plural category"))
        .transpose()?;
    Ok(Plural {
        variant: value.getattr("variant")?.extract()?,
        count: value.getattr("count")?.extract()?,
        category,
        extensions: string_map_from_python(&value.getattr("extensions")?, "extensions")?,
    })
}

fn meta_from_python(value: &Bound<'_, PyAny>) -> PyResult<Meta> {
    Ok(Meta {
        usage_count: value.getattr("usage_count")?.extract()?,
        last_used: value.getattr("last_used")?.extract()?,
        first_used: value.getattr("first_used")?.extract()?,
        created: value.getattr("created")?.extract()?,
        updated: value.getattr("updated")?.extract()?,
        max_length: value.getattr("max_length")?.extract()?,
        min_length: value.getattr("min_length")?.extract()?,
        extensions: string_map_from_python(&value.getattr("extensions")?, "extensions")?,
    })
}

fn origin_from_python(value: &Bound<'_, PyAny>) -> PyResult<Origin> {
    Ok(Origin {
        system: value.getattr("system")?.extract()?,
        project: value.getattr("project")?.extract()?,
        creator_id: value.getattr("creator_id")?.extract()?,
        extensions: string_map_from_python(&value.getattr("extensions")?, "extensions")?,
    })
}

fn comment_from_python(value: &Bound<'_, PyAny>) -> PyResult<Comment> {
    Ok(Comment {
        context: value.getattr("context")?.extract()?,
        timestamp: value.getattr("timestamp")?.extract()?,
        origin: optional_attribute(value, "origin")?
            .map(|item| origin_from_python(&item))
            .transpose()?,
        context_key: value.getattr("context_key")?.extract()?,
        extensions: string_map_from_python(&value.getattr("extensions")?, "extensions")?,
    })
}

fn adjacent_context_from_python(value: &Bound<'_, PyAny>) -> PyResult<AdjacentContext> {
    Ok(AdjacentContext {
        unit_id: value.getattr("unit_id")?.extract()?,
        source: value.getattr("source")?.extract()?,
        target: value.getattr("target")?.extract()?,
        extensions: string_map_from_python(&value.getattr("extensions")?, "extensions")?,
    })
}

fn tags_from_python(value: &Bound<'_, PyAny>) -> PyResult<Tags> {
    Ok(Tags {
        source_tag_map: tie_map_from_python(&value.getattr("source_tag_map")?, "source_tag_map")?,
        target_tag_map: tie_map_from_python(&value.getattr("target_tag_map")?, "target_tag_map")?,
        source_parts: parts_from_python(&value.getattr("source_parts")?)?,
        target_parts: parts_from_python(&value.getattr("target_parts")?)?,
    })
}

fn target_tags_from_python(value: &Bound<'_, PyAny>) -> PyResult<TargetTags> {
    Ok(TargetTags {
        tag_map: tie_map_from_python(&value.getattr("tag_map")?, "tag_map")?,
        parts: parts_from_python(&value.getattr("parts")?)?,
    })
}

fn tie_data_from_python(value: &Bound<'_, PyAny>) -> PyResult<TieData> {
    Ok(TieData {
        id: value.getattr("id")?.extract()?,
        r#type: enum_from_python::<TieType>(&value.getattr("type")?, "tie type")?,
        attributes: string_map_from_python(&value.getattr("attributes")?, "attributes")?,
        attribute_data: value.getattr("attribute_data")?.extract()?,
        position: value.getattr("position")?.extract()?,
        order: value.getattr("order")?.extract()?,
        pair_id: value.getattr("pair_id")?.extract()?,
        original_name: value.getattr("original_name")?.extract()?,
        original_text: value.getattr("original_text")?.extract()?,
    })
}

fn parts_from_python(value: &Bound<'_, PyAny>) -> PyResult<Vec<SegmentPart>> {
    let mut parts = Vec::new();
    for item in value.try_iter()? {
        let item = item?;
        if item.hasattr("value")? {
            parts.push(SegmentPart::Text(TextPart {
                value: item.getattr("value")?.extract()?,
            }));
        } else if item.hasattr("ref")? {
            parts.push(SegmentPart::Code(CodePart {
                r#ref: item.getattr("ref")?.extract()?,
            }));
        } else {
            return Err(PyValueError::new_err(
                "segment part must be a TextPart or CodePart",
            ));
        }
    }
    Ok(parts)
}

fn comments_from_python(value: &Bound<'_, PyAny>) -> PyResult<Vec<Comment>> {
    value
        .try_iter()?
        .map(|item| comment_from_python(&item?))
        .collect()
}

fn targets_from_python(value: &Bound<'_, PyAny>) -> PyResult<Vec<(String, TargetData)>> {
    let dictionary = require_dict(value, "targets")?;
    dictionary
        .iter()
        .map(|(key, value)| Ok((key.extract()?, target_data_from_python(&value)?)))
        .collect()
}

fn tie_map_from_python(value: &Bound<'_, PyAny>, field: &str) -> PyResult<Vec<(String, TieData)>> {
    let dictionary = require_dict(value, field)?;
    dictionary
        .iter()
        .map(|(key, value)| Ok((key.extract()?, tie_data_from_python(&value)?)))
        .collect()
}

fn string_map_from_python(
    value: &Bound<'_, PyAny>,
    field: &str,
) -> PyResult<Vec<(String, String)>> {
    let dictionary = require_dict(value, field)?;
    dictionary
        .iter()
        .map(|(key, value)| Ok((key.extract()?, value.extract()?)))
        .collect()
}

fn require_dict<'a, 'py>(
    value: &'a Bound<'py, PyAny>,
    field: &str,
) -> PyResult<&'a Bound<'py, PyDict>> {
    value
        .cast::<PyDict>()
        .map_err(|_| PyValueError::new_err(format!("{field} must be a dict")))
}

fn optional_attribute<'py>(
    value: &Bound<'py, PyAny>,
    name: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let attribute = value.getattr(name)?;
    Ok((!attribute.is_none()).then_some(attribute))
}

fn translation_status_from_python(value: &Bound<'_, PyAny>) -> PyResult<TranslationStatus> {
    enum_from_python(value, "translation status")
}

fn enum_from_python<T>(value: &Bound<'_, PyAny>, name: &str) -> PyResult<T>
where
    T: FromStr<Err = ()>,
{
    let raw: String = value.extract()?;
    T::from_str(&raw).map_err(|()| PyValueError::new_err(format!("invalid {name}: {raw:?}")))
}

fn reader_to_py_error(error: ReaderFailure) -> PyErr {
    match error {
        ReaderFailure::Io { error, filename } => {
            io_to_py_error_with_filename(error, Some(filename))
        }
        ReaderFailure::Parse(error) if error.code == ErrorCode::Io => {
            PyOSError::new_err(format_parse_error(&error))
        }
        ReaderFailure::Parse(error) => PyValueError::new_err(format_parse_error(&error)),
    }
}

fn format_parse_error(error: &ParseError) -> String {
    format!(
        "{} at line {}, column {}: {}",
        error.code.as_str(),
        error.line(),
        error.column(),
        error.message
    )
}

fn write_to_py_error(error: WriteError) -> PyErr {
    match error {
        WriteError::Io(error) => io_to_py_error(error),
        error @ (WriteError::DuplicateKey { .. }
        | WriteError::InvalidState { .. }
        | WriteError::LineTooLong { .. }) => PyValueError::new_err(error.to_string()),
    }
}

fn io_to_py_error(error: std::io::Error) -> PyErr {
    io_to_py_error_with_filename(error, None)
}

fn io_to_py_error_with_filename(error: std::io::Error, filename: Option<String>) -> PyErr {
    let Some(errno) = error.raw_os_error() else {
        return error.into();
    };
    let message = error.to_string();
    match error.kind() {
        std::io::ErrorKind::BrokenPipe => PyBrokenPipeError::new_err((errno, message, filename)),
        std::io::ErrorKind::ConnectionRefused => {
            PyConnectionRefusedError::new_err((errno, message, filename))
        }
        std::io::ErrorKind::ConnectionAborted => {
            PyConnectionAbortedError::new_err((errno, message, filename))
        }
        std::io::ErrorKind::ConnectionReset => {
            PyConnectionResetError::new_err((errno, message, filename))
        }
        std::io::ErrorKind::Interrupted => PyInterruptedError::new_err((errno, message, filename)),
        std::io::ErrorKind::NotFound => PyFileNotFoundError::new_err((errno, message, filename)),
        std::io::ErrorKind::PermissionDenied => {
            PyPermissionError::new_err((errno, message, filename))
        }
        std::io::ErrorKind::AlreadyExists => PyFileExistsError::new_err((errno, message, filename)),
        std::io::ErrorKind::WouldBlock => PyBlockingIOError::new_err((errno, message, filename)),
        std::io::ErrorKind::TimedOut => PyTimeoutError::new_err((errno, message, filename)),
        std::io::ErrorKind::OutOfMemory => PyMemoryError::new_err(message),
        std::io::ErrorKind::IsADirectory => {
            PyIsADirectoryError::new_err((errno, message, filename))
        }
        std::io::ErrorKind::NotADirectory => {
            PyNotADirectoryError::new_err((errno, message, filename))
        }
        _ => PyOSError::new_err((errno, message, filename)),
    }
}

fn remove_partial_file(path: &Path) {
    let _ = std::fs::remove_file(path);
}

fn remove_partial_file_result(path: &Path) -> std::io::Result<()> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<LokitReader>()?;
    module.add_class::<LokitWriter>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    static NEXT_FILE: AtomicUsize = AtomicUsize::new(0);

    struct TestFile(PathBuf);

    impl TestFile {
        fn new(extension: &str) -> Self {
            let sequence = NEXT_FILE.fetch_add(1, Ordering::Relaxed);
            Self(std::env::temp_dir().join(format!(
                "lokit-python-bridge-{}-{sequence}.{extension}",
                std::process::id()
            )))
        }

        fn with_contents(extension: &str, contents: &str) -> Self {
            let file = Self::new(extension);
            fs::write(file.path(), contents).expect("test input should be writable");
            file
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
    fn streaming_bridge_round_trips_the_complete_model_losslessly() {
        let document = complete_document();
        let mut header = document.clone();
        header.data.clear();
        let output = TestFile::new("lokit");
        let mut writer = open_writer(output.path(), &header).expect("writer should start");
        for (unit_id, data) in &document.data {
            writer
                .write_unit(unit_id, data)
                .expect("unit should be writable");
        }
        writer.finish().expect("writer should flush");

        let mut reader = open_reader(output.path()).expect("reader should open");
        assert_eq!(reader.document_header(), &header);
        assert!(reader.source_map().entries().is_empty());

        let first = read_batch(&mut reader, 1).expect("first batch should parse");
        assert_eq!(first, vec![document.data[0].clone()]);
        assert!(reader.source_map().entries().is_empty());

        let second = read_batch(&mut reader, 1).expect("second batch should parse");
        assert_eq!(second, vec![document.data[1].clone()]);
        assert!(reader.source_map().entries().is_empty());
        assert!(read_batch(&mut reader, 1)
            .expect("end of file should parse")
            .is_empty());

        let encoded = fs::read_to_string(output.path()).expect("output should be UTF-8");
        assert!(encoded.starts_with("@lokit 1\ndocument {\n"));
        assert!(encoded.ends_with('\n'));
        assert!(!encoded.contains("null"));
    }

    #[test]
    fn semantic_tag_inconsistency_round_trips_unchanged() {
        let input = TestFile::with_contents(
            "lokit",
            concat!(
                "@lokit 1\n",
                "document {\n",
                "  source_locale = \"en\"\n",
                "}\n",
                "unit \"broken\" {\n",
                "  source = \"source\"\n",
                "  tags {\n",
                "    source_parts {\n",
                "      code = \"missing\"\n",
                "    }\n",
                "  }\n",
                "}\n",
            ),
        );
        let mut reader = open_reader(input.path()).expect("header should parse");
        assert!(reader.source_map().entries().is_empty());
        let header = reader.document_header().clone();
        let units = read_batch(&mut reader, 1).expect("semantic inconsistency is decodable");
        assert_eq!(units.len(), 1);
        assert!(reader.source_map().entries().is_empty());

        let output = TestFile::new("lokit");
        let mut writer = open_writer(output.path(), &header).expect("writer should start");
        writer
            .write_unit(&units[0].0, &units[0].1)
            .expect("semantic inconsistency is writable");
        writer.finish().expect("writer should finish");

        let mut reparsed = open_reader(output.path()).expect("output should parse");
        assert_eq!(
            read_batch(&mut reparsed, 1).expect("unit should parse"),
            units
        );
        assert!(reparsed.source_map().entries().is_empty());
    }

    #[test]
    fn abort_removes_partial_output_and_is_idempotent() {
        let output = TestFile::new("lokit");
        let document = complete_document();
        let mut header = document.clone();
        header.data.clear();
        let writer = open_writer(output.path(), &header).expect("writer should start");
        let mut bridge = LokitWriter {
            writer: Some(writer),
            path: output.path().to_owned(),
            completed: false,
            aborted: false,
        };
        assert!(output.path().exists());

        bridge.abort_internal();
        bridge.abort_internal();

        assert!(bridge.closed());
        assert_eq!(bridge.state_name(), "aborted");
        assert!(!output.path().exists());
    }

    #[test]
    fn batch_size_bounds_are_enforced() {
        assert!(validate_batch_size(0).is_err());
        assert!(validate_batch_size(1).is_ok());
        assert!(validate_batch_size(DEFAULT_BATCH_SIZE).is_ok());
        assert!(validate_batch_size(MAX_BATCH_SIZE).is_ok());
        assert!(validate_batch_size(MAX_BATCH_SIZE + 1).is_err());
    }

    #[test]
    fn target_filter_promotes_only_the_requested_target() {
        let mut data = Data::new("source");
        data.status = TranslationStatus::Draft;
        data.meta = Meta {
            usage_count: Some(7),
            created: Some("2026-01-01".to_owned()),
            extensions: vec![("shared".to_owned(), "base".to_owned())],
            ..Meta::default()
        };
        data.extensions = vec![("shared".to_owned(), "source".to_owned())];
        data.targets = vec![
            (
                "fr-FR".to_owned(),
                TargetData {
                    text: Some("bonjour".to_owned()),
                    status: TranslationStatus::Approved,
                    extensions: vec![
                        ("shared".to_owned(), "target".to_owned()),
                        ("target".to_owned(), "fr".to_owned()),
                    ],
                    meta: Meta {
                        updated: Some("2026-02-02".to_owned()),
                        extensions: vec![("target".to_owned(), "fr".to_owned())],
                        ..Meta::default()
                    },
                    ..TargetData::default()
                },
            ),
            (
                "de-DE".to_owned(),
                TargetData {
                    text: Some("hallo".to_owned()),
                    ..TargetData::default()
                },
            ),
        ];

        let selected = select_target_data(data.clone(), "fr-FR", None, false)
            .expect("requested target should be retained");
        assert_eq!(selected.target.as_deref(), Some("bonjour"));
        assert_eq!(selected.status, TranslationStatus::Approved);
        assert_eq!(selected.meta.usage_count, Some(7));
        assert_eq!(selected.meta.created.as_deref(), Some("2026-01-01"));
        assert_eq!(selected.meta.updated.as_deref(), Some("2026-02-02"));
        assert_eq!(
            selected.meta.extensions,
            vec![
                ("shared".to_owned(), "base".to_owned()),
                ("target".to_owned(), "fr".to_owned()),
            ]
        );
        assert!(selected.targets.is_empty());
        assert_eq!(
            selected.extensions,
            vec![
                ("shared".to_owned(), "target".to_owned()),
                ("target".to_owned(), "fr".to_owned()),
            ]
        );
        let unknown_status = select_target_data(data.clone(), "de-DE", None, false)
            .expect("requested target should be retained");
        assert_eq!(unknown_status.status, TranslationStatus::Draft);
        assert_eq!(unknown_status.meta.usage_count, Some(7));
        assert!(select_target_data(data.clone(), "es-ES", None, false).is_none());
        assert!(select_target_data(data, "es-ES", None, true).is_some());
    }

    #[test]
    fn parse_error_text_names_line_and_column() {
        let input = TestFile::with_contents("lokit", "document {\n  source_locale = \"en\"\n}\n");
        let error = match open_reader(input.path()) {
            Err(ReaderFailure::Parse(error)) => error,
            Err(_) => panic!("expected a syntax failure"),
            Ok(_) => panic!("missing magic must be rejected"),
        };

        let message = format_parse_error(&error);
        assert!(message.starts_with("LKT005"));
        assert!(message.contains("line 1"));
        assert!(message.contains("column 1"));
    }

    fn complete_document() -> BaseStructure {
        let source_open = TieData {
            id: "source-open-id".to_owned(),
            r#type: TieType::StrongOpen,
            attributes: vec![
                ("class".to_owned(), "hero".to_owned()),
                ("empty".to_owned(), String::new()),
            ],
            attribute_data: "class=hero".to_owned(),
            position: i64::MIN,
            order: i64::MAX,
            pair_id: Some("source-pair".to_owned()),
            original_name: Some(String::new()),
            original_text: Some("<strong>".to_owned()),
        };
        let source_close = TieData {
            id: "source-close-id".to_owned(),
            r#type: TieType::StrongClose,
            attributes: Vec::new(),
            attribute_data: String::new(),
            position: 8,
            order: 2,
            pair_id: Some("source-pair".to_owned()),
            original_name: None,
            original_text: Some("</strong>".to_owned()),
        };
        let target_code = TieData {
            id: "target-code-id".to_owned(),
            r#type: TieType::Br,
            attributes: Vec::new(),
            attribute_data: String::new(),
            position: 0,
            order: 0,
            pair_id: Some(String::new()),
            original_name: Some(String::new()),
            original_text: Some(String::new()),
        };
        let data = Data {
            source: "Hello".to_owned(),
            target: Some(String::new()),
            targets: vec![
                (
                    "fr-FR".to_owned(),
                    TargetData {
                        text: None,
                        status: TranslationStatus::New,
                        tags: Some(TargetTags::default()),
                        plural: Some(Plural {
                            variant: String::new(),
                            count: Some(i64::MIN),
                            category: Some(PluralCategory::Zero),
                            extensions: vec![("target-plural".to_owned(), String::new())],
                        }),
                        meta: Meta {
                            usage_count: Some(0),
                            last_used: Some(String::new()),
                            first_used: None,
                            created: Some("2026-01-01".to_owned()),
                            updated: Some(String::new()),
                            max_length: Some(i64::MAX),
                            min_length: Some(i64::MIN),
                            extensions: vec![("target-meta".to_owned(), "value".to_owned())],
                        },
                        comments: vec![Comment {
                            context: String::new(),
                            timestamp: Some(String::new()),
                            origin: Some(Origin::default()),
                            context_key: Some(String::new()),
                            extensions: vec![("target-comment".to_owned(), String::new())],
                        }],
                        extensions: vec![("target-extension".to_owned(), String::new())],
                    },
                ),
                ("de-DE".to_owned(), TargetData::default()),
            ],
            plural: Some(Plural {
                variant: "Hello variants".to_owned(),
                count: Some(i64::MAX),
                category: Some(PluralCategory::Other),
                extensions: vec![("plural".to_owned(), "source".to_owned())],
            }),
            tags: Some(Tags {
                source_tag_map: vec![
                    ("source-open".to_owned(), source_open),
                    ("source-close".to_owned(), source_close),
                ],
                target_tag_map: vec![("target-code".to_owned(), target_code)],
                source_parts: vec![
                    SegmentPart::Code(CodePart::new("source-open")),
                    SegmentPart::Text(TextPart::new("Hello")),
                    SegmentPart::Code(CodePart::new("source-close")),
                ],
                target_parts: vec![SegmentPart::Code(CodePart::new("target-code"))],
            }),
            meta: Meta {
                usage_count: Some(i64::MIN),
                last_used: Some("2026-07-19T12:34:56Z".to_owned()),
                first_used: Some(String::new()),
                created: None,
                updated: Some(String::new()),
                max_length: Some(i64::MAX),
                min_length: Some(0),
                extensions: vec![
                    ("quality".to_owned(), "gold".to_owned()),
                    ("empty-meta".to_owned(), String::new()),
                ],
            },
            status: TranslationStatus::Reviewed,
            comments: vec![Comment {
                context: "Translator note".to_owned(),
                timestamp: Some(String::new()),
                origin: Some(Origin {
                    system: Some("cms".to_owned()),
                    project: Some(String::new()),
                    creator_id: None,
                    extensions: vec![("origin".to_owned(), "human".to_owned())],
                }),
                context_key: Some("homepage.hero".to_owned()),
                extensions: vec![("audience".to_owned(), "public".to_owned())],
            }],
            previous_context: Some(AdjacentContext::default()),
            next_context: Some(AdjacentContext {
                unit_id: Some(String::new()),
                source: Some("Next".to_owned()),
                target: Some(String::new()),
                extensions: vec![("distance".to_owned(), "1".to_owned())],
            }),
            extensions: vec![
                ("resource".to_owned(), "home".to_owned()),
                ("empty-unit".to_owned(), String::new()),
            ],
        };
        BaseStructure {
            source_locale: String::new(),
            target_locale: Some(String::new()),
            data: vec![
                ("unit/complete".to_owned(), data),
                (
                    "unit/minimal".to_owned(),
                    Data {
                        source: String::new(),
                        tags: Some(Tags::default()),
                        ..Data::new(String::new())
                    },
                ),
            ],
            target_locales: vec!["fr-FR".to_owned(), "de-DE".to_owned()],
            format_version: String::new(),
            export_origin: "bridge-tests".to_owned(),
            export_timestamp: String::new(),
            source_language: Some(String::new()),
            target_language: Some(String::new()),
            target_languages: vec!["fr".to_owned(), "de".to_owned()],
            extensions: vec![
                ("input_format".to_owned(), "xliff".to_owned()),
                ("empty-document".to_owned(), String::new()),
            ],
        }
    }
}
