use std::collections::HashMap;
use std::io::{self, ErrorKind};

const INITIAL_NEXT_SUFFIX: u64 = 2;
const MAX_SUFFIX: u64 = i64::MAX as u64;

#[derive(Debug, Default)]
pub struct BoundedIdRegistry {
    entries: HashMap<Box<str>, u64>,
    #[cfg(test)]
    injected_failure: Option<ErrorKind>,
}

impl BoundedIdRegistry {
    pub fn insert(&mut self, value: &str) -> io::Result<bool> {
        #[cfg(test)]
        self.fail_if_requested()?;
        if self.entries.contains_key(value) {
            return Ok(false);
        }
        self.entries.insert(value.into(), INITIAL_NEXT_SUFFIX);
        Ok(true)
    }

    pub fn contains(&mut self, value: &str) -> io::Result<bool> {
        #[cfg(test)]
        self.fail_if_requested()?;
        Ok(self.entries.contains_key(value))
    }

    pub fn next_suffix(&mut self, value: &str) -> io::Result<u64> {
        let suffix = self
            .entries
            .get_mut(value)
            .ok_or_else(|| io::Error::new(ErrorKind::NotFound, "unit ID is not registered"))?;
        let current = *suffix;
        *suffix = increment_suffix(current)?;
        Ok(current)
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
    fn suffix_counter_overflow_is_checked() {
        let mut registry = BoundedIdRegistry::default();
        assert!(registry
            .insert("overflow")
            .expect("overflow ID should insert"));
        registry.entries.insert("overflow".into(), MAX_SUFFIX);

        let error = registry
            .next_suffix("overflow")
            .expect_err("suffix overflow should fail");

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert_eq!(error.to_string(), "unit ID suffix counter overflow");
    }
}
