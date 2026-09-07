use std::collections::HashMap;
use std::fs::File;
use std::hash::{BuildHasher, RandomState};
use std::io::{self, BufWriter, ErrorKind, Read, Seek, SeekFrom, Write};

const INITIAL_NEXT_SUFFIX: u64 = 2;
const MAX_SUFFIX: u64 = i64::MAX as u64;

// The resident tier is deliberately capped by both cardinality and payload.
// The entry cap bounds HashMap control/bucket storage while the byte cap bounds
// owned ID strings. A single oversized ID bypasses this tier entirely.
const MAX_RESIDENT_ENTRIES: usize = 16_384;
const MAX_RESIDENT_KEY_BYTES: usize = 2 * 1024 * 1024;

const MIN_DISK_SLOTS: u64 = 1_024;
const SLOT_BYTES: u64 = 32;
const LOAD_NUMERATOR: u64 = 7;
const LOAD_DENOMINATOR: u64 = 10;
const KEY_COMPARE_BUFFER_BYTES: usize = 8 * 1024;
const KEY_WRITE_BUFFER_BYTES: usize = 64 * 1024;
const LARGE_REGISTRY_HEADROOM_THRESHOLD: u64 = 4_096;
const LARGE_REGISTRY_HEADROOM_FACTOR: u64 = 8;

/// Exact ID membership and counter storage with a hard-capped resident tier.
///
/// Small registries stay in memory. Once either resident limit is reached, all
/// entries move into securely-created anonymous temporary files. The disk tier
/// is an open-addressed hash table, but hashes are only an index: matching
/// always compares the complete ID bytes, so collisions cannot produce false
/// duplicates. [`RandomState`] gives every registry a keyed hash seed, keeping
/// attacker-selected IDs from creating predictable probe chains.
#[derive(Debug, Default)]
pub struct BoundedIdRegistry {
    entries: HashMap<Box<str>, u64>,
    resident_key_bytes: usize,
    disk: Option<DiskRegistry>,
    limits: ResidentLimits,
    poisoned: bool,
    #[cfg(test)]
    injected_failure: Option<ErrorKind>,
}

impl BoundedIdRegistry {
    pub fn insert(&mut self, value: &str) -> io::Result<bool> {
        Ok(self.get_or_insert(value, INITIAL_NEXT_SUFFIX)?.is_none())
    }

    pub fn contains(&mut self, value: &str) -> io::Result<bool> {
        Ok(self.get(value)?.is_some())
    }

    /// Return the stored value, or atomically register `initial` when absent.
    ///
    /// `None` means the ID was newly registered; `Some(value)` is the exact
    /// value associated with its first registration. This supports streamed
    /// validation retaining a first-unit index without a second unbounded map.
    pub fn get_or_insert(&mut self, value: &str, initial: u64) -> io::Result<Option<u64>> {
        #[cfg(test)]
        self.fail_if_requested()?;
        self.ensure_healthy()?;

        if let Some(disk) = &mut self.disk {
            return disk.get_or_insert(value, initial);
        }
        if let Some(existing) = self.entries.get(value) {
            return Ok(Some(*existing));
        }
        if self.would_exceed_resident_limits(value.len()) {
            self.spill_to_disk()?;
            return self
                .disk
                .as_mut()
                .ok_or_else(invalid_disk_state)?
                .get_or_insert(value, initial);
        }

        self.entries.insert(value.into(), initial);
        self.resident_key_bytes += value.len();
        Ok(None)
    }

    pub fn next_suffix(&mut self, value: &str) -> io::Result<u64> {
        #[cfg(test)]
        self.fail_if_requested()?;
        self.ensure_healthy()?;

        if let Some(disk) = &mut self.disk {
            return disk.next_suffix(value);
        }
        let suffix = self.entries.get_mut(value).ok_or_else(missing_id_error)?;
        let current = *suffix;
        *suffix = increment_suffix(current)?;
        Ok(current)
    }

    pub fn get(&mut self, value: &str) -> io::Result<Option<u64>> {
        #[cfg(test)]
        self.fail_if_requested()?;
        self.ensure_healthy()?;
        match &mut self.disk {
            Some(disk) => disk.get(value),
            None => Ok(self.entries.get(value).copied()),
        }
    }

    fn would_exceed_resident_limits(&self, incoming_bytes: usize) -> bool {
        self.entries.len() >= self.limits.max_entries
            || incoming_bytes
                > self
                    .limits
                    .max_key_bytes
                    .saturating_sub(self.resident_key_bytes)
    }

    fn spill_to_disk(&mut self) -> io::Result<()> {
        let mut disk = match DiskRegistry::new(self.entries.len().saturating_add(1)) {
            Ok(disk) => disk,
            Err(error) => {
                self.poisoned = true;
                return Err(error);
            }
        };
        let entries = std::mem::take(&mut self.entries);
        self.resident_key_bytes = 0;
        for (key, stored) in entries {
            if let Err(error) = disk.insert_known_absent(&key, stored) {
                self.poisoned = true;
                return Err(error);
            }
        }
        self.disk = Some(disk);
        Ok(())
    }

    fn ensure_healthy(&self) -> io::Result<()> {
        if self.poisoned {
            Err(io::Error::other(
                "unit ID registry is unavailable after an I/O failure",
            ))
        } else {
            Ok(())
        }
    }

    #[cfg(test)]
    pub(crate) fn with_limits(max_entries: usize, max_key_bytes: usize) -> Self {
        Self {
            limits: ResidentLimits {
                max_entries,
                max_key_bytes,
            },
            ..Self::default()
        }
    }

    #[cfg(test)]
    fn is_spilled(&self) -> bool {
        self.disk.is_some()
    }

    #[cfg(test)]
    fn resident_usage(&self) -> (usize, usize) {
        (self.entries.len(), self.resident_key_bytes)
    }

    #[cfg(test)]
    fn disk_len(&self) -> u64 {
        self.disk.as_ref().map_or(0, |disk| disk.len)
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

#[derive(Clone, Copy, Debug)]
struct ResidentLimits {
    max_entries: usize,
    max_key_bytes: usize,
}

impl Default for ResidentLimits {
    fn default() -> Self {
        Self {
            max_entries: MAX_RESIDENT_ENTRIES,
            max_key_bytes: MAX_RESIDENT_KEY_BYTES,
        }
    }
}

#[derive(Debug)]
struct DiskRegistry {
    table: File,
    keys: BufWriter<File>,
    hash_builder: RandomState,
    slots: u64,
    len: u64,
    key_bytes: u64,
    key_cursor_displaced: bool,
    poisoned: bool,
    #[cfg(test)]
    forced_hash: Option<u64>,
}

impl DiskRegistry {
    fn new(expected_entries: usize) -> io::Result<Self> {
        let expected = u64::try_from(expected_entries).map_err(|_| registry_size_error())?;
        let slots = table_slots_for(expected)?;
        let table = new_table_file(slots)?;
        let keys = BufWriter::with_capacity(KEY_WRITE_BUFFER_BYTES, tempfile::tempfile()?);
        Ok(Self {
            table,
            keys,
            hash_builder: RandomState::new(),
            slots,
            len: 0,
            key_bytes: 0,
            key_cursor_displaced: false,
            poisoned: false,
            #[cfg(test)]
            forced_hash: None,
        })
    }

    fn get(&mut self, value: &str) -> io::Result<Option<u64>> {
        self.ensure_healthy()?;
        let result = self.find(value).map(|found| match found {
            Lookup::Found(_, slot) => Some(slot.stored),
            Lookup::Vacant(_) => None,
        });
        self.poison_on_error(result)
    }

    fn get_or_insert(&mut self, value: &str, initial: u64) -> io::Result<Option<u64>> {
        self.ensure_healthy()?;
        let result = self.get_or_insert_inner(value, initial);
        self.poison_on_error(result)
    }

    fn get_or_insert_inner(&mut self, value: &str, initial: u64) -> io::Result<Option<u64>> {
        let hash = self.hash(value);
        let lookup = self.find_with_hash(value, hash)?;
        let vacant = match lookup {
            Lookup::Found(_, slot) => return Ok(Some(slot.stored)),
            Lookup::Vacant(index) => index,
        };
        if exceeds_load(self.len.saturating_add(1), self.slots) {
            self.grow()?;
            let index = find_vacant_slot(&mut self.table, self.slots, hash)?;
            self.insert_at(index, hash, value, initial)?;
        } else {
            self.insert_at(vacant, hash, value, initial)?;
        }
        Ok(None)
    }

    fn insert_known_absent(&mut self, value: &str, stored: u64) -> io::Result<()> {
        self.ensure_healthy()?;
        let result = self.insert_known_absent_inner(value, stored);
        self.poison_on_error(result)
    }

    fn insert_known_absent_inner(&mut self, value: &str, stored: u64) -> io::Result<()> {
        if exceeds_load(self.len.saturating_add(1), self.slots) {
            self.grow()?;
        }
        let hash = self.hash(value);
        let index = find_vacant_slot(&mut self.table, self.slots, hash)?;
        self.insert_at(index, hash, value, stored)
    }

    fn insert_at(&mut self, index: u64, hash: u64, value: &str, stored: u64) -> io::Result<()> {
        let key_len = u64::try_from(value.len()).map_err(|_| registry_size_error())?;
        let encoded_len = key_len.checked_add(1).ok_or_else(registry_size_error)?;
        let next_key_bytes = self
            .key_bytes
            .checked_add(key_len)
            .ok_or_else(registry_size_error)?;

        if self.key_cursor_displaced {
            self.keys.get_mut().seek(SeekFrom::Start(self.key_bytes))?;
            self.key_cursor_displaced = false;
        }
        self.keys.write_all(value.as_bytes())?;
        let slot = Slot {
            hash,
            key_offset: self.key_bytes,
            encoded_len,
            stored,
        };
        write_slot(&mut self.table, index, slot)?;
        self.key_bytes = next_key_bytes;
        self.len = self.len.checked_add(1).ok_or_else(registry_size_error)?;
        Ok(())
    }

    fn next_suffix(&mut self, value: &str) -> io::Result<u64> {
        self.ensure_healthy()?;
        let found = self.find(value);
        let (index, mut slot) = match self.poison_on_error(found)? {
            Lookup::Found(index, slot) => (index, slot),
            Lookup::Vacant(_) => return Err(missing_id_error()),
        };
        let current = slot.stored;
        slot.stored = increment_suffix(current)?;
        let result = write_slot(&mut self.table, index, slot).map(|()| current);
        self.poison_on_error(result)
    }

    fn find(&mut self, value: &str) -> io::Result<Lookup> {
        let hash = self.hash(value);
        self.find_with_hash(value, hash)
    }

    fn find_with_hash(&mut self, value: &str, hash: u64) -> io::Result<Lookup> {
        let mut index = hash & (self.slots - 1);
        let value_len = u64::try_from(value.len()).map_err(|_| registry_size_error())?;
        for _ in 0..self.slots {
            let slot = read_slot(&mut self.table, index)?;
            if slot.is_empty() {
                return Ok(Lookup::Vacant(index));
            }
            if slot.hash == hash && slot.key_len() == Some(value_len) {
                self.keys.flush()?;
                self.keys.get_mut().seek(SeekFrom::Start(slot.key_offset))?;
                self.key_cursor_displaced = true;
                if key_equals(self.keys.get_mut(), value.as_bytes())? {
                    return Ok(Lookup::Found(index, slot));
                }
            }
            index = (index + 1) & (self.slots - 1);
        }
        Err(io::Error::new(
            ErrorKind::InvalidData,
            "unit ID registry table has no vacant slot",
        ))
    }

    fn grow(&mut self) -> io::Result<()> {
        let new_slots = self.slots.checked_mul(2).ok_or_else(registry_size_error)?;
        let mut new_table = new_table_file(new_slots)?;
        for old_index in 0..self.slots {
            let slot = read_slot(&mut self.table, old_index)?;
            if !slot.is_empty() {
                let new_index = find_vacant_slot(&mut new_table, new_slots, slot.hash)?;
                write_slot(&mut new_table, new_index, slot)?;
            }
        }
        self.table = new_table;
        self.slots = new_slots;
        Ok(())
    }

    fn hash(&self, value: &str) -> u64 {
        #[cfg(test)]
        if let Some(hash) = self.forced_hash {
            return hash;
        }
        self.hash_builder.hash_one(value)
    }

    fn ensure_healthy(&self) -> io::Result<()> {
        if self.poisoned {
            Err(io::Error::other(
                "unit ID registry disk table is unavailable after an I/O failure",
            ))
        } else {
            Ok(())
        }
    }

    fn poison_on_error<T>(&mut self, result: io::Result<T>) -> io::Result<T> {
        if result.is_err() {
            self.poisoned = true;
        }
        result
    }
}

#[derive(Clone, Copy, Debug)]
struct Slot {
    hash: u64,
    key_offset: u64,
    // Zero is the vacant sentinel; occupied slots store key length plus one so
    // that the empty string remains a valid exact ID.
    encoded_len: u64,
    stored: u64,
}

#[derive(Clone, Copy, Debug)]
enum Lookup {
    Found(u64, Slot),
    Vacant(u64),
}

impl Slot {
    const fn is_empty(self) -> bool {
        self.encoded_len == 0
    }

    const fn key_len(self) -> Option<u64> {
        self.encoded_len.checked_sub(1)
    }
}

fn table_slots_for(expected_entries: u64) -> io::Result<u64> {
    let planned_entries = if expected_entries >= LARGE_REGISTRY_HEADROOM_THRESHOLD {
        expected_entries
            .checked_mul(LARGE_REGISTRY_HEADROOM_FACTOR)
            .ok_or_else(registry_size_error)?
    } else {
        expected_entries
    };
    let mut slots = MIN_DISK_SLOTS;
    while exceeds_load(planned_entries, slots) {
        slots = slots.checked_mul(2).ok_or_else(registry_size_error)?;
    }
    Ok(slots)
}

fn exceeds_load(entries: u64, slots: u64) -> bool {
    entries > slots.saturating_mul(LOAD_NUMERATOR) / LOAD_DENOMINATOR
}

fn new_table_file(slots: u64) -> io::Result<File> {
    let table = tempfile::tempfile()?;
    table.set_len(
        slots
            .checked_mul(SLOT_BYTES)
            .ok_or_else(registry_size_error)?,
    )?;
    Ok(table)
}

fn find_vacant_slot(table: &mut File, slots: u64, hash: u64) -> io::Result<u64> {
    let mut index = hash & (slots - 1);
    for _ in 0..slots {
        if read_slot(table, index)?.is_empty() {
            return Ok(index);
        }
        index = (index + 1) & (slots - 1);
    }
    Err(io::Error::new(
        ErrorKind::InvalidData,
        "unit ID registry table has no vacant slot",
    ))
}

fn read_slot(table: &mut File, index: u64) -> io::Result<Slot> {
    let mut bytes = [0_u8; SLOT_BYTES as usize];
    read_exact_at(table, &mut bytes, slot_offset(index)?)?;
    Ok(Slot {
        hash: decode_u64(&bytes[0..8]),
        key_offset: decode_u64(&bytes[8..16]),
        encoded_len: decode_u64(&bytes[16..24]),
        stored: decode_u64(&bytes[24..32]),
    })
}

fn write_slot(table: &mut File, index: u64, slot: Slot) -> io::Result<()> {
    let mut bytes = [0_u8; SLOT_BYTES as usize];
    bytes[0..8].copy_from_slice(&slot.hash.to_le_bytes());
    bytes[8..16].copy_from_slice(&slot.key_offset.to_le_bytes());
    bytes[16..24].copy_from_slice(&slot.encoded_len.to_le_bytes());
    bytes[24..32].copy_from_slice(&slot.stored.to_le_bytes());
    write_all_at(table, &bytes, slot_offset(index)?)
}

#[cfg(unix)]
fn read_exact_at(file: &File, buffer: &mut [u8], offset: u64) -> io::Result<()> {
    use std::os::unix::fs::FileExt;

    file.read_exact_at(buffer, offset)
}

#[cfg(windows)]
fn read_exact_at(file: &File, buffer: &mut [u8], offset: u64) -> io::Result<()> {
    use std::os::windows::fs::FileExt;

    let mut consumed = 0;
    while consumed < buffer.len() {
        let relative = u64::try_from(consumed).map_err(|_| registry_size_error())?;
        let position = offset
            .checked_add(relative)
            .ok_or_else(registry_size_error)?;
        let count = file.seek_read(&mut buffer[consumed..], position)?;
        if count == 0 {
            return Err(io::Error::new(
                ErrorKind::UnexpectedEof,
                "unit ID registry table ended inside a slot",
            ));
        }
        consumed += count;
    }
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn read_exact_at(file: &mut File, buffer: &mut [u8], offset: u64) -> io::Result<()> {
    file.seek(SeekFrom::Start(offset))?;
    file.read_exact(buffer)
}

#[cfg(unix)]
fn write_all_at(file: &File, buffer: &[u8], offset: u64) -> io::Result<()> {
    use std::os::unix::fs::FileExt;

    file.write_all_at(buffer, offset)
}

#[cfg(windows)]
fn write_all_at(file: &File, buffer: &[u8], offset: u64) -> io::Result<()> {
    use std::os::windows::fs::FileExt;

    let mut consumed = 0;
    while consumed < buffer.len() {
        let relative = u64::try_from(consumed).map_err(|_| registry_size_error())?;
        let position = offset
            .checked_add(relative)
            .ok_or_else(registry_size_error)?;
        let count = file.seek_write(&buffer[consumed..], position)?;
        if count == 0 {
            return Err(io::Error::new(
                ErrorKind::WriteZero,
                "failed to write unit ID registry table slot",
            ));
        }
        consumed += count;
    }
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn write_all_at(file: &mut File, buffer: &[u8], offset: u64) -> io::Result<()> {
    file.seek(SeekFrom::Start(offset))?;
    file.write_all(buffer)
}

fn slot_offset(index: u64) -> io::Result<u64> {
    index
        .checked_mul(SLOT_BYTES)
        .ok_or_else(registry_size_error)
}

fn decode_u64(bytes: &[u8]) -> u64 {
    let mut array = [0_u8; 8];
    array.copy_from_slice(bytes);
    u64::from_le_bytes(array)
}

fn key_equals(keys: &mut File, expected: &[u8]) -> io::Result<bool> {
    let mut buffer = [0_u8; KEY_COMPARE_BUFFER_BYTES];
    let mut offset = 0;
    while offset < expected.len() {
        let count = (expected.len() - offset).min(buffer.len());
        keys.read_exact(&mut buffer[..count])?;
        if buffer[..count] != expected[offset..offset + count] {
            return Ok(false);
        }
        offset += count;
    }
    Ok(true)
}

fn increment_suffix(current: u64) -> io::Result<u64> {
    let next = current.checked_add(1).ok_or_else(suffix_overflow_error)?;
    if next > MAX_SUFFIX {
        return Err(suffix_overflow_error());
    }
    Ok(next)
}

fn missing_id_error() -> io::Error {
    io::Error::new(ErrorKind::NotFound, "unit ID is not registered")
}

fn invalid_disk_state() -> io::Error {
    io::Error::other("unit ID registry failed to initialize its disk table")
}

fn registry_size_error() -> io::Error {
    io::Error::new(ErrorKind::InvalidData, "unit ID registry size overflow")
}

fn suffix_overflow_error() -> io::Error {
    io::Error::new(ErrorKind::InvalidData, "unit ID suffix counter overflow")
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;

    use super::{BoundedIdRegistry, MAX_SUFFIX};

    #[test]
    fn membership_and_suffixes_remain_exact_at_scale() {
        let mut registry = BoundedIdRegistry::default();
        assert!(registry.insert("alpha").expect("alpha should insert"));
        assert!(registry.insert("alpha#2").expect("alpha#2 should insert"));
        assert!(!registry.insert("alpha").expect("alpha should be present"));
        assert_eq!(
            registry
                .next_suffix("alpha")
                .expect("alpha should have a suffix counter"),
            2
        );

        for index in 0..200_000 {
            let value = format!("unit-{index}");
            assert!(registry.insert(&value).expect("unit should insert"));
        }
        assert!(registry.is_spilled());
        assert_eq!(registry.resident_usage(), (0, 0));
        assert_eq!(registry.disk_len(), 200_002);
        for index in (0..200_000).rev() {
            let value = format!("unit-{index}");
            assert!(!registry.insert(&value).expect("unit should be present"));
        }
        assert_eq!(
            registry
                .next_suffix("alpha")
                .expect("suffix counter should remain stable"),
            3
        );
        assert!(registry
            .insert("line\nfeed")
            .expect("newline ID should insert"));
        assert!(!registry
            .insert("line\nfeed")
            .expect("newline ID should be present"));
    }

    #[test]
    fn configured_bounds_force_exact_spill_for_arbitrary_ids_and_values() {
        let mut registry = BoundedIdRegistry::with_limits(3, 12);
        for (key, stored) in [("", 0), ("a\0b", 7), ("é", 9)] {
            assert_eq!(
                registry
                    .get_or_insert(key, stored)
                    .expect("ID should insert"),
                None
            );
        }
        assert_eq!(registry.resident_usage(), (3, 5));

        assert_eq!(
            registry
                .get_or_insert("forces-spill", 11)
                .expect("spill should succeed"),
            None
        );
        assert!(registry.is_spilled());
        assert_eq!(registry.resident_usage(), (0, 0));
        assert_eq!(registry.disk_len(), 4);

        for (key, stored) in [("", 0), ("a\0b", 7), ("é", 9), ("forces-spill", 11)] {
            assert_eq!(
                registry
                    .get_or_insert(key, stored + 100)
                    .expect("duplicate lookup should succeed"),
                Some(stored)
            );
        }
    }

    #[test]
    fn a_single_oversized_id_never_enters_the_resident_tier() {
        let mut registry = BoundedIdRegistry::with_limits(1_000, 8);
        let oversized = "x".repeat(32 * 1024);
        assert!(registry
            .insert(&oversized)
            .expect("oversized ID should spill directly"));
        assert!(registry.is_spilled());
        assert_eq!(registry.resident_usage(), (0, 0));
        assert!(registry
            .contains(&oversized)
            .expect("oversized ID should compare exactly"));
    }

    #[test]
    fn disk_table_growth_preserves_values_and_suffix_counters() {
        let mut registry = BoundedIdRegistry::with_limits(0, 0);
        assert!(registry.insert("counter").expect("counter should insert"));
        for index in 0..20_000_u64 {
            let key = format!("key-{index}");
            assert_eq!(
                registry
                    .get_or_insert(&key, index)
                    .expect("value should insert"),
                None
            );
        }
        for index in (0..20_000_u64).rev() {
            let key = format!("key-{index}");
            assert_eq!(
                registry
                    .get_or_insert(&key, u64::MAX)
                    .expect("value should remain exact"),
                Some(index)
            );
        }
        assert_eq!(
            registry
                .next_suffix("counter")
                .expect("counter should survive table growth"),
            2
        );
    }

    #[test]
    fn full_hash_collisions_still_compare_complete_ids_exactly() {
        let mut registry = BoundedIdRegistry::with_limits(0, 0);
        assert!(registry
            .insert("bootstrap")
            .expect("disk tier should initialize"));
        registry
            .disk
            .as_mut()
            .expect("disk tier should exist")
            .forced_hash = Some(7);

        for index in 0..800_u64 {
            let key = format!("collision-{index}");
            assert_eq!(
                registry
                    .get_or_insert(&key, index)
                    .expect("colliding ID should insert"),
                None
            );
        }
        for index in (0..800_u64).rev() {
            let key = format!("collision-{index}");
            assert_eq!(
                registry
                    .get_or_insert(&key, u64::MAX)
                    .expect("colliding ID should compare exactly"),
                Some(index)
            );
        }
        assert_eq!(registry.disk_len(), 801);
    }

    #[test]
    fn injected_failure_does_not_mutate_the_registry() {
        let mut registry = BoundedIdRegistry::with_limits(0, 0);
        registry.fail_next_operation(ErrorKind::PermissionDenied);
        let error = registry
            .insert("alpha")
            .expect_err("injected insertion should fail");
        assert_eq!(error.kind(), ErrorKind::PermissionDenied);
        assert!(registry.insert("alpha").expect("retry should insert"));
    }

    #[test]
    fn suffix_counter_overflow_is_checked_in_both_tiers() {
        let mut resident = BoundedIdRegistry::default();
        assert!(resident
            .insert("overflow")
            .expect("overflow ID should insert"));
        resident.entries.insert("overflow".into(), MAX_SUFFIX);

        let error = resident
            .next_suffix("overflow")
            .expect_err("resident suffix overflow should fail");
        assert_eq!(error.kind(), ErrorKind::InvalidData);
        assert_eq!(error.to_string(), "unit ID suffix counter overflow");

        let mut disk = BoundedIdRegistry::with_limits(0, 0);
        assert_eq!(
            disk.get_or_insert("overflow", MAX_SUFFIX)
                .expect("disk ID should insert"),
            None
        );
        let error = disk
            .next_suffix("overflow")
            .expect_err("disk suffix overflow should fail");
        assert_eq!(error.kind(), ErrorKind::InvalidData);
        assert_eq!(error.to_string(), "unit ID suffix counter overflow");
    }
}
