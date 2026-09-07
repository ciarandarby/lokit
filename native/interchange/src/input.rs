use std::fs::File;
use std::io::{self, BufRead, BufReader, Cursor, Read};
use std::path::Path;

pub(crate) struct Input {
    reader: BufReader<Box<dyn Read + Send + Sync>>,
    remaining: usize,
}

impl Input {
    pub(crate) fn path(path: &Path) -> io::Result<Self> {
        Ok(Self::new(Box::new(File::open(path)?)))
    }

    pub(crate) fn bytes(data: Vec<u8>) -> Self {
        Self::new(Box::new(Cursor::new(data)))
    }

    fn new(source: Box<dyn Read + Send + Sync>) -> Self {
        Self {
            reader: BufReader::with_capacity(crate::READ_CAPACITY, source),
            remaining: crate::MAX_COMPLEX_UNIT_BYTES as usize,
        }
    }

    pub(crate) fn limit(&mut self, bytes: usize) {
        self.remaining = bytes;
    }
}

impl Read for Input {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let data = self.fill_buf()?;
        let count = data.len().min(output.len());
        output[..count].copy_from_slice(&data[..count]);
        self.consume(count);
        Ok(count)
    }
}

impl BufRead for Input {
    fn fill_buf(&mut self) -> io::Result<&[u8]> {
        if self.remaining == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "XML event or translation unit exceeds 64 MiB",
            ));
        }
        let data = self.reader.fill_buf()?;
        Ok(&data[..data.len().min(self.remaining)])
    }

    fn consume(&mut self, bytes: usize) {
        self.remaining = self.remaining.saturating_sub(bytes);
        self.reader.consume(bytes);
    }
}
