use std::error::Error;
use std::fmt;
use std::sync::Arc;

use tower_lsp_server::ls_types::{
    Position, PositionEncodingKind, Range, TextDocumentContentChangeEvent,
};

const MAX_CHANGE_EVENTS: usize = 4_096;
const MIN_INCREMENTAL_EDIT_WORK_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum PositionEncoding {
    Utf8,
    #[default]
    Utf16,
    Utf32,
}

impl PositionEncoding {
    pub(crate) fn select(offered: Option<&[PositionEncodingKind]>) -> Self {
        let Some(encodings) = offered else {
            return Self::Utf16;
        };
        for encoding in encodings {
            if encoding == &PositionEncodingKind::UTF8 {
                return Self::Utf8;
            }
            if encoding == &PositionEncodingKind::UTF16 {
                return Self::Utf16;
            }
            if encoding == &PositionEncodingKind::UTF32 {
                return Self::Utf32;
            }
        }
        // UTF-16 is mandatory for LSP clients, even when omitted from the list.
        Self::Utf16
    }

    pub(crate) const fn as_lsp(self) -> PositionEncodingKind {
        match self {
            Self::Utf8 => PositionEncodingKind::UTF8,
            Self::Utf16 => PositionEncodingKind::UTF16,
            Self::Utf32 => PositionEncodingKind::UTF32,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Document {
    text: Arc<str>,
    line_index: Arc<LineIndex>,
    version: i32,
}

impl Document {
    pub(crate) fn new(text: String, version: i32) -> Self {
        let line_index = Arc::new(LineIndex::new(&text));
        Self {
            text: Arc::from(text),
            line_index,
            version,
        }
    }

    #[cfg(test)]
    pub(crate) fn text(&self) -> &str {
        &self.text
    }

    pub(crate) fn shared_text(&self) -> Arc<str> {
        Arc::clone(&self.text)
    }

    pub(crate) fn shared_line_index(&self) -> Arc<LineIndex> {
        Arc::clone(&self.line_index)
    }

    pub(crate) const fn version(&self) -> i32 {
        self.version
    }

    pub(crate) fn apply_changes(
        &mut self,
        changes: &[TextDocumentContentChangeEvent],
        version: i32,
        encoding: PositionEncoding,
        maximum_bytes: usize,
    ) -> Result<(), ChangeError> {
        if version <= self.version {
            return Err(ChangeError::StaleVersion {
                current: self.version,
                received: version,
            });
        }
        if changes.len() > MAX_CHANGE_EVENTS {
            return Err(ChangeError::TooManyChanges {
                maximum_changes: MAX_CHANGE_EVENTS,
            });
        }

        let mut updated = self.text.to_string();
        let mut updated_index = (*self.line_index).clone();
        let mut edit_work_bytes = 0_usize;
        let maximum_edit_work_bytes = maximum_bytes.max(MIN_INCREMENTAL_EDIT_WORK_BYTES);
        for change in changes {
            if let Some(range) = change.range {
                let start = updated_index
                    .byte_offset(&updated, range.start, encoding)
                    .ok_or(ChangeError::InvalidRange(range))?;
                let end = updated_index
                    .byte_offset(&updated, range.end, encoding)
                    .ok_or(ChangeError::InvalidRange(range))?;
                if start > end {
                    return Err(ChangeError::InvalidRange(range));
                }
                let resulting_bytes = updated
                    .len()
                    .checked_sub(end - start)
                    .and_then(|length| length.checked_add(change.text.len()))
                    .ok_or(ChangeError::DocumentTooLarge { maximum_bytes })?;
                if resulting_bytes > maximum_bytes {
                    return Err(ChangeError::DocumentTooLarge { maximum_bytes });
                }
                charge_edit_work(
                    &mut edit_work_bytes,
                    updated.len().max(resulting_bytes),
                    maximum_edit_work_bytes,
                )?;
                updated.replace_range(start..end, &change.text);
                updated_index = LineIndex::new(&updated);
            } else {
                if change.text.len() > maximum_bytes {
                    return Err(ChangeError::DocumentTooLarge { maximum_bytes });
                }
                charge_edit_work(
                    &mut edit_work_bytes,
                    change.text.len(),
                    maximum_edit_work_bytes,
                )?;
                updated.clone_from(&change.text);
                updated_index = LineIndex::new(&updated);
            }
        }

        self.text = Arc::from(updated);
        self.line_index = Arc::new(updated_index);
        self.version = version;
        Ok(())
    }
}

fn charge_edit_work(
    total: &mut usize,
    bytes: usize,
    maximum_bytes: usize,
) -> Result<(), ChangeError> {
    *total = total
        .checked_add(bytes)
        .ok_or(ChangeError::EditWorkLimit { maximum_bytes })?;
    if *total > maximum_bytes {
        return Err(ChangeError::EditWorkLimit { maximum_bytes });
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ChangeError {
    StaleVersion { current: i32, received: i32 },
    InvalidRange(Range),
    DocumentTooLarge { maximum_bytes: usize },
    TooManyChanges { maximum_changes: usize },
    EditWorkLimit { maximum_bytes: usize },
    ResyncRequired,
}

impl fmt::Display for ChangeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::StaleVersion { current, received } => write!(
                formatter,
                "stale document version {received}; current version is {current}"
            ),
            Self::InvalidRange(range) => write!(
                formatter,
                "invalid document range {}:{}-{}:{}",
                range.start.line, range.start.character, range.end.line, range.end.character
            ),
            Self::DocumentTooLarge { maximum_bytes } => write!(
                formatter,
                "document exceeds the configured {maximum_bytes}-byte limit"
            ),
            Self::TooManyChanges { maximum_changes } => write!(
                formatter,
                "change event exceeds the configured {maximum_changes}-edit limit"
            ),
            Self::EditWorkLimit { maximum_bytes } => write!(
                formatter,
                "incremental change exceeds the configured {maximum_bytes}-byte work limit"
            ),
            Self::ResyncRequired => formatter.write_str(
                "the document is out of sync; send one bounded full-content replacement to resynchronize",
            ),
        }
    }
}

impl Error for ChangeError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LineIndex {
    line_starts: Box<[usize]>,
    text_bytes: usize,
}

impl LineIndex {
    pub(crate) fn new(text: &str) -> Self {
        let bytes = text.as_bytes();
        let mut line_starts = Vec::with_capacity(
            bytes
                .iter()
                .filter(|byte| matches!(byte, b'\n' | b'\r'))
                .count()
                .saturating_add(1),
        );
        line_starts.push(0);
        let mut index = 0_usize;
        while index < bytes.len() {
            match bytes[index] {
                b'\r' => {
                    index += 1;
                    if bytes.get(index) == Some(&b'\n') {
                        index += 1;
                    }
                    line_starts.push(index);
                }
                b'\n' => {
                    index += 1;
                    line_starts.push(index);
                }
                _ => index += 1,
            }
        }
        Self {
            line_starts: line_starts.into_boxed_slice(),
            text_bytes: bytes.len(),
        }
    }

    pub(crate) fn byte_offset(
        &self,
        text: &str,
        position: Position,
        encoding: PositionEncoding,
    ) -> Option<usize> {
        let requested_line = usize::try_from(position.line).ok()?;
        let (line_start, line_end) = self.line_bounds(text, requested_line)?;
        let line = text.get(line_start..line_end)?;
        let requested = usize::try_from(position.character).ok()?;

        let relative = match encoding {
            PositionEncoding::Utf8 => {
                if requested >= line.len() {
                    line.len()
                } else if line.is_char_boundary(requested) {
                    requested
                } else {
                    return None;
                }
            }
            PositionEncoding::Utf16 => offset_for_units(line, requested, char::len_utf16)?,
            PositionEncoding::Utf32 => offset_for_units(line, requested, |_| 1)?,
        };
        line_start.checked_add(relative)
    }

    pub(crate) fn position_at_byte(
        &self,
        text: &str,
        byte: usize,
        encoding: PositionEncoding,
    ) -> Option<Position> {
        if text.len() != self.text_bytes || byte > text.len() || !text.is_char_boundary(byte) {
            return None;
        }
        let line = self
            .line_starts
            .partition_point(|line_start| *line_start <= byte)
            .saturating_sub(1);
        let (line_start, line_end) = self.line_bounds(text, line)?;
        let content_byte = byte.min(line_end);
        let line_prefix = text.get(line_start..content_byte)?;
        let character = match encoding {
            PositionEncoding::Utf8 => line_prefix.len(),
            PositionEncoding::Utf16 => line_prefix.encode_utf16().count(),
            PositionEncoding::Utf32 => line_prefix.chars().count(),
        };
        Some(Position::new(
            saturating_u32(line),
            saturating_u32(character),
        ))
    }

    pub(crate) fn range_for_bytes(
        &self,
        text: &str,
        start: usize,
        end: usize,
        encoding: PositionEncoding,
    ) -> Option<Range> {
        if start > end {
            return None;
        }
        Some(Range::new(
            self.position_at_byte(text, start, encoding)?,
            self.position_at_byte(text, end, encoding)?,
        ))
    }

    pub(crate) fn full_document_range(&self, text: &str, encoding: PositionEncoding) -> Range {
        let end = self
            .position_at_byte(text, text.len(), encoding)
            .unwrap_or_default();
        Range::new(Position::default(), end)
    }

    pub(crate) fn line_count(&self) -> usize {
        self.line_starts.len()
    }

    pub(crate) fn line_bounds(&self, text: &str, line: usize) -> Option<(usize, usize)> {
        if text.len() != self.text_bytes {
            return None;
        }
        let start = *self.line_starts.get(line)?;
        let mut end = self.line_full_end(line)?;
        if end > start && text.as_bytes().get(end - 1) == Some(&b'\n') {
            end -= 1;
        }
        if end > start && text.as_bytes().get(end - 1) == Some(&b'\r') {
            end -= 1;
        }
        Some((start, end))
    }

    pub(crate) fn line_full_end(&self, line: usize) -> Option<usize> {
        self.line_starts
            .get(line + 1)
            .copied()
            .or_else(|| (line + 1 == self.line_starts.len()).then_some(self.text_bytes))
    }
}

fn offset_for_units(line: &str, requested: usize, width: impl Fn(char) -> usize) -> Option<usize> {
    let mut units = 0_usize;
    for (byte, character) in line.char_indices() {
        if units == requested {
            return Some(byte);
        }
        let next = units.checked_add(width(character))?;
        if requested < next {
            return None;
        }
        units = next;
    }
    Some(line.len())
}

fn saturating_u32(value: usize) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, Instant};

    use super::*;

    fn change(range: Option<Range>, text: &str) -> TextDocumentContentChangeEvent {
        TextDocumentContentChangeEvent {
            range,
            range_length: None,
            text: text.to_owned(),
        }
    }

    #[test]
    fn converts_positions_for_all_negotiated_encodings() {
        let text = "a😀é\nβ";
        let index = LineIndex::new(text);
        assert_eq!(
            index.position_at_byte(text, "a😀".len(), PositionEncoding::Utf8),
            Some(Position::new(0, 5))
        );
        assert_eq!(
            index.position_at_byte(text, "a😀".len(), PositionEncoding::Utf16),
            Some(Position::new(0, 3))
        );
        assert_eq!(
            index.position_at_byte(text, "a😀".len(), PositionEncoding::Utf32),
            Some(Position::new(0, 2))
        );
        assert_eq!(
            index.byte_offset(text, Position::new(0, 3), PositionEncoding::Utf16),
            Some("a😀".len())
        );
        assert_eq!(
            index.byte_offset(text, Position::new(1, 1), PositionEncoding::Utf16),
            Some(text.len())
        );
    }

    #[test]
    fn rejects_a_position_inside_a_surrogate_pair() {
        let index = LineIndex::new("😀");
        assert_eq!(
            index.byte_offset("😀", Position::new(0, 1), PositionEncoding::Utf16),
            None
        );
    }

    #[test]
    fn applies_incremental_changes_in_order() {
        let mut document = Document::new("a😀c".to_owned(), 1);
        let changes = [
            change(
                Some(Range::new(Position::new(0, 1), Position::new(0, 3))),
                "b",
            ),
            change(
                Some(Range::new(Position::new(0, 2), Position::new(0, 3))),
                "d",
            ),
        ];
        assert_eq!(
            document.apply_changes(&changes, 2, PositionEncoding::Utf16, 100),
            Ok(())
        );
        assert_eq!(document.text(), "abd");
        assert_eq!(document.version(), 2);
    }

    #[test]
    fn changes_are_atomic_on_an_invalid_range() {
        let mut document = Document::new("abc".to_owned(), 4);
        let changes = [
            change(
                Some(Range::new(Position::new(0, 0), Position::new(0, 1))),
                "z",
            ),
            change(
                Some(Range::new(Position::new(2, 0), Position::new(2, 1))),
                "x",
            ),
        ];
        assert!(
            document
                .apply_changes(&changes, 5, PositionEncoding::Utf16, 100)
                .is_err()
        );
        assert_eq!(document.text(), "abc");
        assert_eq!(document.version(), 4);
    }

    #[test]
    fn rejects_stale_versions_and_oversized_replacements() {
        let mut document = Document::new("abc".to_owned(), 2);
        assert_eq!(
            document.apply_changes(&[], 2, PositionEncoding::Utf16, 10),
            Err(ChangeError::StaleVersion {
                current: 2,
                received: 2,
            })
        );
        assert_eq!(
            document.apply_changes(
                &[change(None, "01234567890")],
                3,
                PositionEncoding::Utf16,
                10,
            ),
            Err(ChangeError::DocumentTooLarge { maximum_bytes: 10 })
        );
    }

    #[test]
    fn treats_crlf_as_a_line_ending() {
        let text = "abc\r\ndef";
        let index = LineIndex::new(text);
        assert_eq!(
            index.byte_offset(text, Position::new(0, 3), PositionEncoding::Utf16),
            Some(3)
        );
        assert_eq!(
            index.byte_offset(text, Position::new(1, 0), PositionEncoding::Utf16),
            Some(5)
        );
        assert_eq!(
            index.position_at_byte(text, 4, PositionEncoding::Utf16),
            Some(Position::new(0, 3))
        );
    }

    #[test]
    fn treats_bare_carriage_returns_as_line_endings() {
        let text = "a😀\rβ\rgamma";
        let index = LineIndex::new(text);
        assert_eq!(index.line_count(), 3);
        assert_eq!(
            index.byte_offset(text, Position::new(1, 2), PositionEncoding::Utf8),
            Some("a😀\rβ".len())
        );
        assert_eq!(
            index.byte_offset(text, Position::new(0, 3), PositionEncoding::Utf16),
            Some("a😀".len())
        );
        assert_eq!(
            index.position_at_byte(text, "a😀\r".len(), PositionEncoding::Utf32),
            Some(Position::new(1, 0))
        );
    }

    #[test]
    fn caps_many_incremental_edits_before_work_becomes_quadratic() {
        let original = "x".repeat(2 * 1024 * 1024);
        let mut document = Document::new(original.clone(), 1);
        let changes: Vec<_> = (0..40)
            .map(|_| {
                change(
                    Some(Range::new(Position::new(0, 0), Position::new(0, 0))),
                    "y",
                )
            })
            .collect();
        let started = Instant::now();
        assert_eq!(
            document.apply_changes(&changes, 2, PositionEncoding::Utf16, 16 * 1024 * 1024,),
            Err(ChangeError::EditWorkLimit {
                maximum_bytes: MIN_INCREMENTAL_EDIT_WORK_BYTES,
            })
        );
        assert_eq!(document.text(), original);
        assert_eq!(document.version(), 1);
        assert!(started.elapsed() < Duration::from_secs(2));

        let excessive: Vec<_> = (0..=MAX_CHANGE_EVENTS)
            .map(|_| change(None, "small"))
            .collect();
        assert_eq!(
            document.apply_changes(&excessive, 2, PositionEncoding::Utf16, 100),
            Err(ChangeError::TooManyChanges {
                maximum_changes: MAX_CHANGE_EVENTS,
            })
        );
    }

    #[test]
    fn caps_aggregate_full_replacement_work_atomically() {
        let original = "original".to_owned();
        let replacement = "x".repeat(6 * 1024 * 1024);
        let mut document = Document::new(original.clone(), 1);
        let changes = [
            change(None, &replacement),
            change(None, &replacement),
            change(None, &replacement),
        ];
        assert_eq!(
            document.apply_changes(&changes, 2, PositionEncoding::Utf16, 16 * 1024 * 1024),
            Err(ChangeError::EditWorkLimit {
                maximum_bytes: MIN_INCREMENTAL_EDIT_WORK_BYTES,
            })
        );
        assert_eq!(document.text(), original);
        assert_eq!(document.version(), 1);

        let mut single = Document::new(String::new(), 1);
        assert_eq!(
            single.apply_changes(
                &[change(None, &"y".repeat(MIN_INCREMENTAL_EDIT_WORK_BYTES))],
                2,
                PositionEncoding::Utf16,
                MIN_INCREMENTAL_EDIT_WORK_BYTES,
            ),
            Ok(())
        );
    }

    #[test]
    fn large_documents_accept_one_incremental_edit() {
        const LARGE_DOCUMENT_BYTES: usize = 17 * 1024 * 1024;

        let mut document = Document::new("x".repeat(LARGE_DOCUMENT_BYTES), 1);
        let changes = [change(
            Some(Range::new(Position::new(0, 0), Position::new(0, 1))),
            "y",
        )];

        assert_eq!(
            document.apply_changes(&changes, 2, PositionEncoding::Utf16, 128 * 1024 * 1024,),
            Ok(())
        );
        assert_eq!(document.version(), 2);
        assert!(document.text().starts_with('y'));
    }
}
