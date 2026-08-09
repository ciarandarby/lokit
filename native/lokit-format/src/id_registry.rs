use std::collections::hash_map::RandomState;
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::hash::BuildHasher;
use std::io::{self, ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const MEMORY_ENTRY_LIMIT: usize = 65_536;
const MEMORY_BYTE_LIMIT: usize = 8 * 1024 * 1024;
const MIN_DISK_CAPACITY: usize = 1_024;
const INDEX_SLOT_BYTES: u64 = 8;
const COMPARE_BUFFER_BYTES: usize = 8 * 1024;
const MAX_TEMP_DIRECTORY_ATTEMPTS: usize = 128;
const INITIAL_NEXT_SUFFIX: u64 = 2;
const MAX_SUFFIX: u64 = i64::MAX as u64;

static NEXT_TEMP_DIRECTORY: AtomicU64 = AtomicU64::new(0);

#[derive(Debug)]
pub struct BoundedIdRegistry {
    memory: HashMap<String, u64>,
    memory_bytes: usize,
    disk: Option<DiskIdSet>,
    entry_limit: usize,
    byte_limit: usize,
    #[cfg(test)]
    injected_failure: Option<ErrorKind>,
}

impl Default for BoundedIdRegistry {
    fn default() -> Self {
        Self::with_limits(MEMORY_ENTRY_LIMIT, MEMORY_BYTE_LIMIT)
    }
}

impl BoundedIdRegistry {
    #[doc(hidden)]
    pub fn with_limits(entry_limit: usize, byte_limit: usize) -> Self {
        Self {
            memory: HashMap::new(),
            memory_bytes: 0,
            disk: None,
            entry_limit,
            byte_limit,
            #[cfg(test)]
            injected_failure: None,
        }
    }

    pub fn insert(&mut self, value: &str) -> io::Result<bool> {
        #[cfg(test)]
        self.fail_if_requested()?;
        if let Some(disk) = self.disk.as_mut() {
            return disk.insert(value);
        }
        if self.memory.contains_key(value) {
            return Ok(false);
        }
        if self.memory.len() < self.entry_limit
            && self.memory_bytes.saturating_add(value.len()) <= self.byte_limit
        {
            self.memory.insert(value.to_owned(), INITIAL_NEXT_SUFFIX);
            self.memory_bytes += value.len();
            return Ok(true);
        }

        let mut disk = DiskIdSet::create(self.memory.len().saturating_add(1))?;
        for (existing, next_suffix) in &self.memory {
            let inserted = disk.insert_with_next_suffix(existing, *next_suffix)?;
            debug_assert!(inserted);
        }
        let inserted = disk.insert(value)?;
        self.memory = HashMap::new();
        self.memory_bytes = 0;
        self.disk = Some(disk);
        Ok(inserted)
    }

    pub fn contains(&mut self, value: &str) -> io::Result<bool> {
        #[cfg(test)]
        self.fail_if_requested()?;
        if let Some(disk) = self.disk.as_mut() {
            return disk.contains(value);
        }
        Ok(self.memory.contains_key(value))
    }

    pub fn next_suffix(&mut self, value: &str) -> io::Result<u64> {
        if let Some(disk) = self.disk.as_mut() {
            return disk.next_suffix(value);
        }
        let suffix = self
            .memory
            .get_mut(value)
            .ok_or_else(|| io::Error::new(ErrorKind::NotFound, "unit ID is not registered"))?;
        let current = *suffix;
        *suffix = increment_suffix(current)?;
        Ok(current)
    }

    #[doc(hidden)]
    pub fn is_spilled(&self) -> bool {
        self.disk.is_some()
    }

    #[doc(hidden)]
    pub fn temporary_directory(&self) -> Option<&Path> {
        self.disk
            .as_ref()
            .map(|disk| disk._directory.path.as_path())
    }

    #[cfg(test)]
    pub(crate) fn fail_next_operation(&mut self, kind: ErrorKind) {
        self.injected_failure = Some(kind);
    }

    #[cfg(test)]
    fn fail_if_requested(&mut self) -> io::Result<()> {
        match self.injected_failure.take() {
            Some(kind) => Err(io::Error::new(kind, "injected unit ID registry failure")),
            None => Ok(()),
        }
    }
}

#[derive(Debug)]
struct DiskIdSet {
    index: File,
    data: File,
    capacity: usize,
    len: usize,
    hash_builder: RandomState,
    _directory: TemporaryDirectory,
}

enum Probe {
    Vacant(usize),
    Occupied(u64),
}

impl DiskIdSet {
    fn create(expected_ids: usize) -> io::Result<Self> {
        let directory = TemporaryDirectory::create()?;
        let index = OpenOptions::new()
            .create_new(true)
            .read(true)
            .write(true)
            .open(directory.path.join("index"))?;
        let data = OpenOptions::new()
            .create_new(true)
            .read(true)
            .write(true)
            .open(directory.path.join("ids"))?;
        let capacity = disk_capacity(expected_ids)?;
        index.set_len(index_bytes(capacity)?)?;
        Ok(Self {
            index,
            data,
            capacity,
            len: 0,
            hash_builder: RandomState::new(),
            _directory: directory,
        })
    }

    fn insert(&mut self, value: &str) -> io::Result<bool> {
        self.insert_with_next_suffix(value, INITIAL_NEXT_SUFFIX)
    }

    fn contains(&mut self, value: &str) -> io::Result<bool> {
        let hash = self.hash_builder.hash_one(value.as_bytes());
        Ok(matches!(self.probe(hash, value)?, Probe::Occupied(_)))
    }

    fn insert_with_next_suffix(&mut self, value: &str, next_suffix: u64) -> io::Result<bool> {
        let hash = self.hash_builder.hash_one(value.as_bytes());
        let empty_slot = match self.probe(hash, value)? {
            Probe::Vacant(slot) => slot,
            Probe::Occupied(_) => return Ok(false),
        };
        if exceeds_load_limit(self.len.saturating_add(1), self.capacity) {
            self.resize()?;
            return self.insert_with_next_suffix(value, next_suffix);
        }

        let data_offset = self.append_record(value, next_suffix)?;
        self.write_slot(empty_slot, data_offset)?;
        self.len += 1;
        Ok(true)
    }

    fn next_suffix(&mut self, value: &str) -> io::Result<u64> {
        let hash = self.hash_builder.hash_one(value.as_bytes());
        let data_offset = match self.probe(hash, value)? {
            Probe::Occupied(offset) => offset,
            Probe::Vacant(_) => {
                return Err(io::Error::new(
                    ErrorKind::NotFound,
                    "unit ID is not registered",
                ));
            }
        };
        let suffix_offset = data_offset.checked_add(8).ok_or_else(|| {
            io::Error::new(ErrorKind::FileTooLarge, "unit ID data file is too large")
        })?;
        self.data.seek(SeekFrom::Start(suffix_offset))?;
        let current = read_u64(&mut self.data)?;
        let next = increment_suffix(current)?;
        self.data.seek(SeekFrom::Start(suffix_offset))?;
        self.data.write_all(&next.to_le_bytes())?;
        Ok(current)
    }

    fn probe(&mut self, hash: u64, value: &str) -> io::Result<Probe> {
        let mask = self.capacity - 1;
        let mut slot = hash as usize & mask;
        for _ in 0..self.capacity {
            match self.read_slot(slot)? {
                None => return Ok(Probe::Vacant(slot)),
                Some(offset) if self.record_equals(offset, value.as_bytes())? => {
                    return Ok(Probe::Occupied(offset));
                }
                Some(_) => slot = (slot + 1) & mask,
            }
        }
        Err(io::Error::other("unit ID disk index is full"))
    }

    fn resize(&mut self) -> io::Result<()> {
        let capacity = self.capacity.checked_mul(2).ok_or_else(|| {
            io::Error::new(ErrorKind::FileTooLarge, "unit ID disk index is too large")
        })?;
        self.index.set_len(0)?;
        self.index.set_len(index_bytes(capacity)?)?;
        self.capacity = capacity;
        self.data.seek(SeekFrom::Start(0))?;

        let mut value = Vec::new();
        for _ in 0..self.len {
            let offset = self.data.stream_position()?;
            let value_length = read_u64(&mut self.data)?;
            let _next_suffix = read_u64(&mut self.data)?;
            let value_length = usize::try_from(value_length).map_err(|_| {
                io::Error::new(
                    ErrorKind::InvalidData,
                    "unit ID length does not fit in memory",
                )
            })?;
            value.resize(value_length, 0);
            self.data.read_exact(&mut value)?;
            let hash = self.hash_builder.hash_one(value.as_slice());
            self.insert_offset(hash, offset)?;
        }
        Ok(())
    }

    fn insert_offset(&mut self, hash: u64, data_offset: u64) -> io::Result<()> {
        let mask = self.capacity - 1;
        let mut slot = hash as usize & mask;
        for _ in 0..self.capacity {
            if self.read_slot(slot)?.is_none() {
                return self.write_slot(slot, data_offset);
            }
            slot = (slot + 1) & mask;
        }
        Err(io::Error::other("unit ID disk index is full"))
    }

    fn read_slot(&mut self, slot: usize) -> io::Result<Option<u64>> {
        self.index.seek(SeekFrom::Start(slot_offset(slot)?))?;
        let stored = read_u64(&mut self.index)?;
        if stored == 0 {
            Ok(None)
        } else {
            Ok(Some(stored - 1))
        }
    }

    fn write_slot(&mut self, slot: usize, data_offset: u64) -> io::Result<()> {
        let stored = data_offset.checked_add(1).ok_or_else(|| {
            io::Error::new(ErrorKind::FileTooLarge, "unit ID data file is too large")
        })?;
        self.index.seek(SeekFrom::Start(slot_offset(slot)?))?;
        self.index.write_all(&stored.to_le_bytes())
    }

    fn append_record(&mut self, value: &str, next_suffix: u64) -> io::Result<u64> {
        let value_length = u64::try_from(value.len()).map_err(|_| {
            io::Error::new(
                ErrorKind::InvalidInput,
                "unit ID length does not fit on disk",
            )
        })?;
        let offset = self.data.seek(SeekFrom::End(0))?;
        self.data.write_all(&value_length.to_le_bytes())?;
        self.data.write_all(&next_suffix.to_le_bytes())?;
        self.data.write_all(value.as_bytes())?;
        Ok(offset)
    }

    fn record_equals(&mut self, offset: u64, expected: &[u8]) -> io::Result<bool> {
        self.data.seek(SeekFrom::Start(offset))?;
        let stored_length = read_u64(&mut self.data)?;
        let _next_suffix = read_u64(&mut self.data)?;
        let expected_length = u64::try_from(expected.len()).map_err(|_| {
            io::Error::new(
                ErrorKind::InvalidInput,
                "unit ID length does not fit on disk",
            )
        })?;
        if stored_length != expected_length {
            return Ok(false);
        }

        let mut buffer = [0_u8; COMPARE_BUFFER_BYTES];
        let mut compared = 0;
        while compared < expected.len() {
            let chunk_length = (expected.len() - compared).min(buffer.len());
            self.data.read_exact(&mut buffer[..chunk_length])?;
            if buffer[..chunk_length] != expected[compared..compared + chunk_length] {
                return Ok(false);
            }
            compared += chunk_length;
        }
        Ok(true)
    }
}

#[derive(Debug)]
struct TemporaryDirectory {
    path: PathBuf,
}

impl TemporaryDirectory {
    fn create() -> io::Result<Self> {
        let root = std::env::temp_dir();
        for _ in 0..MAX_TEMP_DIRECTORY_ATTEMPTS {
            let sequence = NEXT_TEMP_DIRECTORY.fetch_add(1, Ordering::Relaxed);
            let path = root.join(format!("lokit-unit-ids-{}-{sequence}", std::process::id()));
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            match builder.create(&path) {
                Ok(()) => return Ok(Self { path }),
                Err(error) if error.kind() == ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        Err(io::Error::new(
            ErrorKind::AlreadyExists,
            "could not allocate a temporary unit ID directory",
        ))
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

fn disk_capacity(expected_ids: usize) -> io::Result<usize> {
    let doubled = expected_ids.max(1).checked_mul(2).ok_or_else(|| {
        io::Error::new(ErrorKind::FileTooLarge, "unit ID disk index is too large")
    })?;
    doubled
        .checked_next_power_of_two()
        .map(|value| value.max(MIN_DISK_CAPACITY))
        .ok_or_else(|| io::Error::new(ErrorKind::FileTooLarge, "unit ID disk index is too large"))
}

fn index_bytes(capacity: usize) -> io::Result<u64> {
    u64::try_from(capacity)
        .ok()
        .and_then(|value| value.checked_mul(INDEX_SLOT_BYTES))
        .ok_or_else(|| io::Error::new(ErrorKind::FileTooLarge, "unit ID disk index is too large"))
}

fn slot_offset(slot: usize) -> io::Result<u64> {
    u64::try_from(slot)
        .ok()
        .and_then(|value| value.checked_mul(INDEX_SLOT_BYTES))
        .ok_or_else(|| io::Error::new(ErrorKind::FileTooLarge, "unit ID disk index is too large"))
}

fn read_u64(reader: &mut File) -> io::Result<u64> {
    let mut bytes = [0_u8; 8];
    reader.read_exact(&mut bytes)?;
    Ok(u64::from_le_bytes(bytes))
}

fn exceeds_load_limit(entries: usize, capacity: usize) -> bool {
    entries.saturating_mul(10) > capacity.saturating_mul(7)
}

fn increment_suffix(current: u64) -> io::Result<u64> {
    let next = current.checked_add(1).ok_or_else(suffix_overflow_error)?;
    if next > MAX_SUFFIX {
        return Err(suffix_overflow_error());
    }
    Ok(next)
}

fn suffix_overflow_error() -> io::Error {
    io::Error::new(ErrorKind::InvalidData, "unit ID suffix counter overflow")
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::{BoundedIdRegistry, MAX_SUFFIX};

    #[test]
    fn spill_is_exact_across_resizes_and_removes_temporary_files() {
        let mut registry = BoundedIdRegistry::with_limits(2, 16);
        assert!(registry.insert("alpha").expect("alpha should insert"));
        assert!(registry.insert("alpha#2").expect("alpha#2 should insert"));
        assert!(!registry.insert("alpha").expect("alpha should be present"));
        assert_eq!(
            registry
                .next_suffix("alpha")
                .expect("alpha should have a suffix counter"),
            2
        );
        assert!(registry.insert("beta").expect("beta should trigger spill"));
        assert!(registry.is_spilled());
        assert_eq!(
            registry
                .next_suffix("alpha")
                .expect("spill should preserve the suffix counter"),
            3
        );

        for index in 0..2_000 {
            let value = format!("unit-{index}");
            assert!(registry.insert(&value).expect("unit should insert"));
        }
        for index in (0..2_000).rev() {
            let value = format!("unit-{index}");
            assert!(!registry.insert(&value).expect("unit should be present"));
        }
        assert_eq!(
            registry
                .next_suffix("alpha")
                .expect("resize should preserve the suffix counter"),
            4
        );
        assert!(registry
            .insert("line\nfeed")
            .expect("newline ID should insert"));
        assert!(!registry
            .insert("line\nfeed")
            .expect("newline ID should be present"));

        let directory = registry
            .temporary_directory()
            .expect("spill should own a temporary directory")
            .to_owned();
        assert!(directory.is_dir());
        drop(registry);
        assert!(!directory.exists());
        assert!(fs::metadata(directory).is_err());
    }

    #[test]
    fn suffix_counter_overflow_is_checked() {
        let mut registry = BoundedIdRegistry::with_limits(2, 16);
        assert!(registry
            .insert("overflow")
            .expect("overflow ID should insert"));
        registry.memory.insert("overflow".to_owned(), MAX_SUFFIX);

        let error = registry
            .next_suffix("overflow")
            .expect_err("suffix overflow should fail");

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert_eq!(error.to_string(), "unit ID suffix counter overflow");
    }
}
