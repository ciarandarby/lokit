use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Read, Seek, SeekFrom, Write};

use lokit_format::id_registry::BoundedIdRegistry;
use lokit_format::{Data, TargetData, TranslationStatus};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;

use crate::lokit::PythonClasses;

const MAX_VALUE_BYTES: usize = 64 * 1024 * 1024;
const INDEX_BYTES: usize = 2 * 1024 * 1024;

struct Object {
    first: bool,
    keys: BoundedIdRegistry,
}

struct JsonInput {
    input: BufReader<File>,
    offset: u64,
    stack: Vec<Object>,
    path: Vec<String>,
    roots: Vec<String>,
    collect_roots: bool,
    finished: bool,
}

impl JsonInput {
    fn open(path: &str) -> PyResult<Self> {
        let mut input = BufReader::with_capacity(crate::READ_CAPACITY, File::open(path)?);
        if input.fill_buf()?.starts_with(&[0xef, 0xbb, 0xbf]) {
            input.consume(3);
        }
        let mut reader = Self {
            input,
            offset: 0,
            stack: Vec::new(),
            path: Vec::new(),
            roots: Vec::new(),
            collect_roots: false,
            finished: false,
        };
        reader.whitespace()?;
        if reader.peek()? != Some(b'{') {
            return Err(PyTypeError::new_err(
                "Expected JSON object at translation root",
            ));
        }
        reader.take()?;
        reader.stack.push(Object {
            first: true,
            keys: BoundedIdRegistry::default(),
        });
        Ok(reader)
    }

    fn fail(&self, message: &str) -> PyErr {
        PyValueError::new_err(format!("{message} at JSON byte {}", self.offset))
    }

    fn peek(&mut self) -> PyResult<Option<u8>> {
        Ok(self.input.fill_buf()?.first().copied())
    }

    fn take(&mut self) -> PyResult<u8> {
        let value = self
            .peek()?
            .ok_or_else(|| self.fail("Unexpected end of input"))?;
        self.input.consume(1);
        self.offset += 1;
        Ok(value)
    }

    fn whitespace(&mut self) -> PyResult<()> {
        while self
            .peek()?
            .is_some_and(|byte| matches!(byte, b' ' | b'\r' | b'\n' | b'\t'))
        {
            self.take()?;
        }
        Ok(())
    }

    fn expect(&mut self, wanted: u8) -> PyResult<()> {
        self.whitespace()?;
        if self.take()? != wanted {
            return Err(self.fail(&format!("Expected {:?}", char::from(wanted))));
        }
        Ok(())
    }

    fn string(&mut self) -> PyResult<String> {
        self.expect(b'"')?;
        let mut bytes = vec![b'"'];
        let mut escaped = false;
        loop {
            let byte = self.take()?;
            bytes.push(byte);
            if bytes.len() > MAX_VALUE_BYTES {
                return Err(self.fail("JSON string exceeds 64 MiB"));
            }
            if byte == b'"' && !escaped {
                break;
            }
            escaped = byte == b'\\' && !escaped;
        }
        serde_json::from_slice(&bytes).map_err(|error| self.fail(&error.to_string()))
    }

    fn skip(&mut self, depth: usize) -> PyResult<()> {
        if depth >= 256 {
            return Err(self.fail("JSON nesting exceeds the 256-level safety limit"));
        }
        self.whitespace()?;
        match self.peek()? {
            Some(b'"') => {
                self.string()?;
            }
            Some(b'{') | Some(b'[') => {
                let object = self.take()? == b'{';
                let end = if object { b'}' } else { b']' };
                self.whitespace()?;
                if self.peek()? == Some(end) {
                    self.take()?;
                    return Ok(());
                }
                let mut keys = BoundedIdRegistry::default();
                loop {
                    if object {
                        let key = self.string()?;
                        if !keys.insert(&key)? {
                            return Err(self.fail(&format!("Duplicate JSON member {key:?}")));
                        }
                        self.expect(b':')?;
                    }
                    self.skip(depth + 1)?;
                    self.whitespace()?;
                    let separator = self.take()?;
                    if separator == end {
                        break;
                    }
                    if separator != b',' {
                        return Err(self.fail("Expected a JSON separator"));
                    }
                }
            }
            Some(_) => {
                let mut bytes = Vec::new();
                while self.peek()?.is_some_and(|byte| {
                    !matches!(byte, b',' | b'}' | b']' | b' ' | b'\r' | b'\n' | b'\t')
                }) {
                    bytes.push(self.take()?);
                    if bytes.len() > MAX_VALUE_BYTES {
                        return Err(self.fail("JSON scalar exceeds 64 MiB"));
                    }
                }
                serde_json::from_slice::<serde_json::Value>(&bytes)
                    .map_err(|error| self.fail(&error.to_string()))?;
            }
            None => return Err(self.fail("Unexpected end of JSON")),
        }
        Ok(())
    }

    fn next(&mut self) -> PyResult<Option<(Vec<String>, String)>> {
        if self.finished {
            return Ok(None);
        }
        loop {
            self.whitespace()?;
            let first = self.stack.last().expect("active object").first;
            if self.peek()? == Some(b'}') {
                self.take()?;
                self.stack.pop();
                self.path.pop();
                if self.stack.is_empty() {
                    self.whitespace()?;
                    if self.peek()?.is_some() {
                        return Err(self.fail("Trailing JSON data"));
                    }
                    self.finished = true;
                    return Ok(None);
                }
                continue;
            }
            if !first {
                self.expect(b',')?;
            }
            let key = self.string()?;
            let object = self.stack.last_mut().expect("active object");
            object.first = false;
            if !object.keys.insert(&key)? {
                return Err(self.fail(&format!("Duplicate JSON member {key:?}")));
            }
            self.expect(b':')?;
            self.whitespace()?;
            match self.peek()? {
                Some(b'{') => {
                    if self.stack.len() >= 256 {
                        return Err(self.fail("JSON nesting exceeds the 256-level safety limit"));
                    }
                    if self.collect_roots && self.path.is_empty() && key.len() <= 32 {
                        if self.roots.len() >= 4096 {
                            return Err(self.fail("JSON locale discovery exceeds 4096 object roots; specify monolingual target files to bypass discovery"));
                        }
                        self.roots.push(key.clone());
                    }
                    self.take()?;
                    self.path.push(key);
                    self.stack.push(Object {
                        first: true,
                        keys: BoundedIdRegistry::default(),
                    });
                }
                Some(b'"') => {
                    let value = self.string()?;
                    let mut path = self.path.clone();
                    path.push(key);
                    return Ok(Some((path, value)));
                }
                _ => self.skip(self.stack.len())?,
            }
        }
    }
}

struct Index {
    resident: HashMap<String, String>,
    bytes: usize,
    disk: Option<(BoundedIdRegistry, BufWriter<File>, u64)>,
}

impl Index {
    fn new() -> Self {
        Self {
            resident: HashMap::new(),
            bytes: 0,
            disk: None,
        }
    }

    fn insert(&mut self, key: String, text: String) -> PyResult<()> {
        if self.disk.is_none()
            && (self.bytes.saturating_add(key.len() + text.len()) > INDEX_BYTES
                || self.resident.len() >= 16_384)
        {
            self.disk = Some((
                BoundedIdRegistry::default(),
                BufWriter::with_capacity(crate::READ_CAPACITY, tempfile::tempfile()?),
                0,
            ));
            let resident = std::mem::take(&mut self.resident);
            for (key, text) in resident {
                self.insert(key, text)?;
            }
            self.bytes = 0;
        }
        if let Some((registry, output, offset)) = &mut self.disk {
            if registry.get_or_insert(&key, *offset)?.is_some() {
                return Err(PyValueError::new_err("Duplicate structural JSON target"));
            }
            output.write_all(&(text.len() as u64).to_le_bytes())?;
            output.write_all(text.as_bytes())?;
            *offset += 8 + text.len() as u64;
        } else {
            self.bytes += key.len() + text.len();
            self.resident.insert(key, text);
        }
        Ok(())
    }

    fn get(&mut self, key: &str) -> PyResult<Option<String>> {
        if let Some((registry, output, _)) = &mut self.disk {
            let Some(offset) = registry.get(key)? else {
                return Ok(None);
            };
            output.flush()?;
            let file = output.get_mut();
            file.seek(SeekFrom::Start(offset))?;
            let mut bytes = [0; 8];
            file.read_exact(&mut bytes)?;
            let length = usize::try_from(u64::from_le_bytes(bytes))
                .map_err(|_| PyValueError::new_err("JSON index value length overflow"))?;
            if length > MAX_VALUE_BYTES {
                return Err(PyValueError::new_err("JSON index value exceeds 64 MiB"));
            }
            let mut bytes = vec![0; length];
            file.read_exact(&mut bytes)?;
            Ok(Some(String::from_utf8(bytes).map_err(|error| {
                PyValueError::new_err(error.to_string())
            })?))
        } else {
            Ok(self.resident.get(key).cloned())
        }
    }
}

fn path_key(path: &[String]) -> PyResult<String> {
    serde_json::to_string(path).map_err(|error| PyValueError::new_err(error.to_string()))
}

#[pyfunction]
fn json_object_roots(py: Python<'_>, path: String) -> PyResult<Vec<String>> {
    py.detach(move || {
        let mut reader = JsonInput::open(&path)?;
        reader.collect_roots = true;
        while reader.next()?.is_some() {}
        Ok(reader.roots)
    })
}

#[pyclass(module = "lokit._interchange_rust")]
struct JsonReader {
    source: Option<JsonInput>,
    prefix: Option<String>,
    targets: Vec<(String, Index)>,
    selected: Option<String>,
    ids: BoundedIdRegistry,
    error: Option<PyErr>,
}

#[pymethods]
impl JsonReader {
    #[new]
    #[pyo3(signature = (path, source_root=None, targets=Vec::new(), selected_target=None))]
    fn new(
        py: Python<'_>,
        path: String,
        source_root: Option<String>,
        targets: Vec<(String, String, Option<String>)>,
        selected_target: Option<String>,
    ) -> PyResult<Self> {
        py.detach(move || {
            let mut indices = Vec::with_capacity(targets.len());
            for (locale, path, prefix) in targets {
                let mut index = Index::new();
                let mut reader = JsonInput::open(&path)?;
                while let Some((path, text)) = reader.next()? {
                    let path = if let Some(prefix) = &prefix {
                        if path.first() != Some(prefix) {
                            continue;
                        }
                        &path[1..]
                    } else {
                        &path[..]
                    };
                    index.insert(path_key(path)?, text)?;
                }
                indices.push((locale, index));
            }
            Ok(Self {
                source: Some(JsonInput::open(&path)?),
                prefix: source_root,
                targets: indices,
                selected: selected_target,
                ids: BoundedIdRegistry::default(),
                error: None,
            })
        })
    }

    #[pyo3(signature = (runtime_placeholders=false, inline_placeholders=false, syntaxes=None, batch_size=256))]
    fn read_batch(
        &mut self,
        py: Python<'_>,
        runtime_placeholders: bool,
        inline_placeholders: bool,
        syntaxes: Option<Vec<String>>,
        batch_size: usize,
    ) -> PyResult<Vec<(String, Py<PyAny>)>> {
        if batch_size == 0 || batch_size > crate::MAX_BATCH_SIZE {
            return Err(PyValueError::new_err(
                "batch_size must be between 1 and 16384",
            ));
        }
        if let Some(error) = self.error.take() {
            return Err(error);
        }
        let options = crate::placeholder::projection_options(
            runtime_placeholders,
            inline_placeholders,
            syntaxes,
        )?;
        let records = py.detach(|| {
            let mut records = Vec::with_capacity(batch_size.min(crate::DEFAULT_BATCH_SIZE));
            let mut bytes = 0usize;
            while records.len() < batch_size && bytes < crate::MAX_BATCH_BYTES {
                match self.next_data() {
                    Ok(Some((key, data))) => {
                        match crate::placeholder::project_native(data, &options) {
                            Ok(data) => {
                                bytes = bytes
                                    .saturating_add(key.capacity())
                                    .saturating_add(data.retained_bytes());
                                records.push((key, data));
                            }
                            Err(error) => {
                                self.close();
                                self.error = Some(error);
                                break;
                            }
                        }
                    }
                    Ok(None) => break,
                    Err(error) => {
                        self.close();
                        self.error = Some(error);
                        break;
                    }
                }
            }
            if records.is_empty() {
                if let Some(error) = self.error.take() {
                    return Err(error);
                }
            }
            Ok(records)
        })?;
        let classes = PythonClasses::import(py)?;
        records
            .into_iter()
            .map(|(key, data)| classes.data(py, data).map(|data| (key, data.unbind())))
            .collect()
    }

    #[getter]
    fn closed(&self) -> bool {
        self.source.is_none()
    }

    fn close(&mut self) {
        self.source = None;
        self.targets.clear();
        self.ids = BoundedIdRegistry::default();
    }
}

impl JsonReader {
    fn next_data(&mut self) -> PyResult<Option<(String, Data)>> {
        loop {
            let Some(source) = &mut self.source else {
                return Ok(None);
            };
            let Some((path, text)) = source.next()? else {
                self.close();
                return Ok(None);
            };
            let path = if let Some(prefix) = &self.prefix {
                if path.first() != Some(prefix) {
                    continue;
                }
                &path[1..]
            } else {
                &path[..]
            };
            let key = path_key(path)?;
            let unit_id = crate::identity::resolve_id(&mut self.ids, &path.join("."))?;
            let mut data = Data::new(text);
            data.status = TranslationStatus::New;
            data.extensions = vec![
                ("input_format".into(), "json_i18n".into()),
                ("json_path".into(), key.clone()),
            ];
            for (locale, index) in &mut self.targets {
                let text = index.get(&key)?;
                let status = if text.as_ref().is_some_and(|text| !text.is_empty()) {
                    TranslationStatus::Translated
                } else {
                    TranslationStatus::New
                };
                if self.selected.as_deref() == Some(locale.as_str()) {
                    data.target = text;
                    data.status = status;
                } else if self.selected.is_none() && !locale.is_empty() {
                    data.targets.push((
                        locale.clone(),
                        TargetData {
                            text,
                            status,
                            ..TargetData::default()
                        },
                    ));
                }
            }
            return Ok(Some((unit_id, data)));
        }
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<JsonReader>()?;
    module.add_function(wrap_pyfunction!(json_object_roots, module)?)?;
    Ok(())
}
