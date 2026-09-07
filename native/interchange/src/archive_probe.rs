use std::fs::File;
use std::io::{self, BufReader, Cursor, Read, Seek, SeekFrom};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const MAX_ENTRIES: u64 = 100_000;
const MAX_DIRECTORY_BYTES: u64 = 64 * 1024 * 1024;
const MAX_CONTENT_TYPES_BYTES: u64 = 2 * 1024 * 1024;

struct BufferedInput<R> {
    input: BufReader<R>,
    position: u64,
}

impl<R: Read + Seek> BufferedInput<R> {
    fn new(mut input: R) -> io::Result<Self> {
        let position = input.stream_position()?;
        Ok(Self {
            input: BufReader::with_capacity(8192, input),
            position,
        })
    }
}

impl<R: Read> Read for BufferedInput<R> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let count = self.input.read(output)?;
        self.position += count as u64;
        Ok(count)
    }
}

impl<R: Read + Seek> Seek for BufferedInput<R> {
    fn seek(&mut self, from: SeekFrom) -> io::Result<u64> {
        let relative = match from {
            SeekFrom::Current(offset) => Some(offset),
            SeekFrom::Start(position) => {
                i64::try_from(i128::from(position) - i128::from(self.position)).ok()
            }
            SeekFrom::End(_) => None,
        };
        if let Some(offset) = relative {
            let position = self.position.checked_add_signed(offset).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "Invalid archive seek")
            })?;
            self.input.seek_relative(offset)?;
            self.position = position;
        } else {
            self.position = self.input.seek(from)?;
        }
        Ok(self.position)
    }

    fn stream_position(&mut self) -> io::Result<u64> {
        Ok(self.position)
    }
}

enum ProbeError {
    Invalid,
    Rejected(&'static str),
}

impl From<io::Error> for ProbeError {
    fn from(_: io::Error) -> Self {
        Self::Invalid
    }
}

fn u16_at(bytes: &[u8], offset: usize) -> u16 {
    u16::from_le_bytes(
        bytes[offset..offset + 2]
            .try_into()
            .expect("fixed ZIP field"),
    )
}

fn u32_at(bytes: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(
        bytes[offset..offset + 4]
            .try_into()
            .expect("fixed ZIP field"),
    )
}

fn u64_at(bytes: &[u8], offset: usize) -> u64 {
    u64::from_le_bytes(
        bytes[offset..offset + 8]
            .try_into()
            .expect("fixed ZIP field"),
    )
}

struct Directory {
    format: Option<&'static str>,
    idml: bool,
    content_types: Option<String>,
}

fn directory<R: Read + Seek>(input: &mut R) -> Result<Directory, ProbeError> {
    let file_end = input.seek(SeekFrom::End(0))?;
    let tail_start = file_end.saturating_sub(65557);
    input.seek(SeekFrom::Start(tail_start))?;
    let mut tail = vec![0; (file_end - tail_start) as usize];
    input.read_exact(&mut tail)?;
    let eocd = (0..tail.len().saturating_sub(21))
        .rev()
        .find(|&offset| {
            tail[offset..].starts_with(b"PK\x05\x06")
                && offset + 22 + usize::from(u16_at(&tail, offset + 20)) == tail.len()
        })
        .ok_or(ProbeError::Invalid)?;
    let eocd_bytes = &tail[eocd..];
    if u16_at(eocd_bytes, 4) != 0 || u16_at(eocd_bytes, 6) != 0 {
        return Err(ProbeError::Invalid);
    }
    let mut entries = u64::from(u16_at(eocd_bytes, 10));
    let mut size = u64::from(u32_at(eocd_bytes, 12));
    let mut end = tail_start + eocd as u64;
    if entries == u64::from(u16::MAX)
        || size == u64::from(u32::MAX)
        || u32_at(eocd_bytes, 16) == u32::MAX
    {
        let locator_offset = end.checked_sub(20).ok_or(ProbeError::Invalid)?;
        input.seek(SeekFrom::Start(locator_offset))?;
        let mut locator = [0u8; 20];
        input.read_exact(&mut locator)?;
        if locator.starts_with(b"PK\x06\x07") {
            if u32_at(&locator, 4) != 0 || u32_at(&locator, 16) != 1 {
                return Err(ProbeError::Invalid);
            }
            end = u64_at(&locator, 8);
            if end
                .checked_add(56)
                .is_none_or(|value| value > locator_offset)
            {
                return Err(ProbeError::Invalid);
            }
            input.seek(SeekFrom::Start(end))?;
            let mut zip64 = [0u8; 56];
            input.read_exact(&mut zip64)?;
            if !zip64.starts_with(b"PK\x06\x06")
                || u32_at(&zip64, 16) != 0
                || u32_at(&zip64, 20) != 0
            {
                return Err(ProbeError::Invalid);
            }
            entries = u64_at(&zip64, 32);
            size = u64_at(&zip64, 40);
        } else if size == u64::from(u32::MAX) || u32_at(eocd_bytes, 16) == u32::MAX {
            return Err(ProbeError::Invalid);
        }
    }
    if entries > MAX_ENTRIES {
        return Err(ProbeError::Rejected(
            "ZIP input exceeds the 100000-entry format-detection limit",
        ));
    }
    if size > MAX_DIRECTORY_BYTES {
        return Err(ProbeError::Rejected(
            "ZIP central directory exceeds the 64 MiB format-detection limit",
        ));
    }
    let mut position = end.checked_sub(size).ok_or(ProbeError::Invalid)?;
    input.seek(SeekFrom::Start(position))?;
    let mut result = Directory {
        format: None,
        idml: false,
        content_types: None,
    };
    let mut header = [0u8; 46];
    let mut name = Vec::new();
    let mut count = 0;
    while position < end {
        count += 1;
        if count > MAX_ENTRIES {
            return Err(ProbeError::Rejected(
                "ZIP input exceeds the 100000-entry format-detection limit",
            ));
        }
        if end - position < header.len() as u64 {
            return Err(ProbeError::Invalid);
        }
        input.read_exact(&mut header)?;
        if !header.starts_with(b"PK\x01\x02") {
            return Err(ProbeError::Invalid);
        }
        let name_len = usize::from(u16_at(&header, 28));
        let rest_len = u64::from(u16_at(&header, 30)) + u64::from(u16_at(&header, 32));
        position += header.len() as u64 + name_len as u64 + rest_len;
        if position > end {
            return Err(ProbeError::Invalid);
        }
        name.resize(name_len, 0);
        input.read_exact(&mut name)?;
        result.idml |= name.starts_with(b"Stories/");
        name.make_ascii_lowercase();
        if name == b"vbaproject.bin" || name.ends_with(b"/vbaproject.bin") {
            return Err(ProbeError::Rejected(
                "Macro-enabled Office files are not supported",
            ));
        }
        if name == b"[content_types].xml" {
            if result.content_types.is_some() {
                return Err(ProbeError::Rejected(
                    "Office ZIP contains duplicate [Content_Types].xml entries",
                ));
            }
            if u32_at(&header, 24) != u32::MAX
                && u64::from(u32_at(&header, 24)) > MAX_CONTENT_TYPES_BYTES
            {
                return Err(ProbeError::Rejected("Could not detect input format: Office [Content_Types].xml exceeds the format-detection size limit"));
            }
            input.seek_relative(-(name_len as i64))?;
            input.read_exact(&mut name)?;
            result.content_types =
                Some(String::from_utf8(name.clone()).map_err(|_| ProbeError::Invalid)?);
        } else if name == b"word/document.xml" {
            result.format = Some("docx");
        } else if name == b"ppt/presentation.xml" && result.format != Some("docx") {
            result.format = Some("pptx");
        } else if name == b"xl/workbook.xml" && result.format.is_none() {
            result.format = Some("xlsx");
        }
        if rest_len != 0 {
            input.seek_relative(rest_len as i64)?;
        }
    }
    if count != entries {
        return Err(ProbeError::Invalid);
    }
    Ok(result)
}

fn detect<R: Read + Seek>(mut input: R) -> Result<Option<&'static str>, ProbeError> {
    let directory = directory(&mut input)?;
    if let Some(name) = directory.content_types {
        input.rewind()?;
        let mut archive = zip::ZipArchive::new(input).map_err(|_| ProbeError::Invalid)?;
        let entry = archive.by_name(&name).map_err(|_| ProbeError::Invalid)?;
        if entry.size() > MAX_CONTENT_TYPES_BYTES {
            return Err(ProbeError::Rejected("Could not detect input format: Office [Content_Types].xml exceeds the format-detection size limit"));
        }
        let mut content = Vec::with_capacity((entry.size() as usize).min(8192));
        entry
            .take(MAX_CONTENT_TYPES_BYTES + 1)
            .read_to_end(&mut content)?;
        if content.len() as u64 > MAX_CONTENT_TYPES_BYTES {
            return Err(ProbeError::Rejected("Could not detect input format: Office [Content_Types].xml exceeds the format-detection size limit"));
        }
        content.make_ascii_lowercase();
        if content.windows(12).any(|value| value == b"macroenabled") {
            return Err(ProbeError::Rejected(
                "Macro-enabled Office files are not supported",
            ));
        }
        if directory.format.is_none() {
            for (pattern, format) in [
                (b"wordprocessingml.document.main+xml".as_slice(), "docx"),
                (b"presentationml.presentation.main+xml".as_slice(), "pptx"),
                (b"spreadsheetml.sheet.main+xml".as_slice(), "xlsx"),
            ] {
                if content.windows(pattern.len()).any(|value| value == pattern) {
                    return Ok(Some(format));
                }
            }
        }
    }
    Ok(directory
        .format
        .or(if directory.idml { Some("idml") } else { None }))
}

fn result(value: Result<Option<&'static str>, ProbeError>) -> PyResult<Option<&'static str>> {
    match value {
        Ok(format) => Ok(format),
        Err(ProbeError::Invalid) => Ok(None),
        Err(ProbeError::Rejected(message)) => Err(PyValueError::new_err(message)),
    }
}

#[pyfunction]
fn detect_archive_path(py: Python<'_>, path: &str) -> PyResult<Option<&'static str>> {
    result(py.detach(|| detect(BufferedInput::new(File::open(path)?)?)))
}

#[pyfunction]
fn detect_archive_bytes(py: Python<'_>, data: &[u8]) -> PyResult<Option<&'static str>> {
    result(py.detach(|| detect(Cursor::new(data))))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(detect_archive_path, module)?)?;
    module.add_function(wrap_pyfunction!(detect_archive_bytes, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct CountedInput {
        cursor: Cursor<Vec<u8>>,
        seeks: usize,
    }

    impl Read for CountedInput {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            self.cursor.read(output)
        }
    }

    impl Seek for CountedInput {
        fn seek(&mut self, position: SeekFrom) -> io::Result<u64> {
            self.seeks += 1;
            self.cursor.seek(position)
        }
    }

    #[test]
    fn buffered_seeks_preserve_position_without_repeated_system_calls() {
        let inner = CountedInput {
            cursor: Cursor::new((0..=255).cycle().take(32768).collect()),
            seeks: 0,
        };
        let mut input = BufferedInput::new(inner).unwrap();
        let mut bytes = [0u8; 5];
        input.read_exact(&mut bytes).unwrap();
        assert_eq!(bytes, [0, 1, 2, 3, 4]);
        assert_eq!(input.seek(SeekFrom::Start(3)).unwrap(), 3);
        input.read_exact(&mut bytes).unwrap();
        assert_eq!(bytes, [3, 4, 5, 6, 7]);
        assert_eq!(input.stream_position().unwrap(), 8);
        assert_eq!(input.seek(SeekFrom::Current(1)).unwrap(), 9);
        assert_eq!(input.seek(SeekFrom::Current(-1)).unwrap(), 8);
        assert_eq!(input.input.get_ref().seeks, 1);
        assert!(input.seek(SeekFrom::Current(-9)).is_err());
        assert_eq!(input.stream_position().unwrap(), 8);
        assert_eq!(input.seek(SeekFrom::End(0)).unwrap(), 32768);
        assert_eq!(input.seek(SeekFrom::Start(1024)).unwrap(), 1024);
        input.read_exact(&mut bytes).unwrap();
        assert_eq!(bytes, [0, 1, 2, 3, 4]);
    }

    #[test]
    fn directory_scanning_retains_buffered_input() {
        let mut bytes = Vec::new();
        for index in 0..300 {
            let name = format!("media/{index}.txt");
            let mut header = [0u8; 46];
            header[..4].copy_from_slice(b"PK\x01\x02");
            header[28..30].copy_from_slice(&(name.len() as u16).to_le_bytes());
            header[32..34].copy_from_slice(&3u16.to_le_bytes());
            bytes.extend_from_slice(&header);
            bytes.extend_from_slice(name.as_bytes());
            bytes.extend_from_slice(b"abc");
        }
        let mut end = [0u8; 22];
        end[..4].copy_from_slice(b"PK\x05\x06");
        end[8..10].copy_from_slice(&300u16.to_le_bytes());
        end[10..12].copy_from_slice(&300u16.to_le_bytes());
        end[12..16].copy_from_slice(&(bytes.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&end);
        let mut input = BufReader::with_capacity(
            8192,
            CountedInput {
                cursor: Cursor::new(bytes),
                seeks: 0,
            },
        );
        assert!(directory(&mut input).is_ok());
        assert!(input.get_ref().seeks < 10);
    }
}
