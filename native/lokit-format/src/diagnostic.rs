use std::collections::HashMap;
use std::error::Error;
use std::fmt;
use std::ops::Range;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SourcePosition {
    /// One-based line number.
    pub line: usize,
    /// One-based Unicode scalar column.
    pub column: usize,
    /// Zero-based UTF-8 byte offset.
    pub byte: usize,
}

impl SourcePosition {
    pub const fn new(line: usize, column: usize, byte: usize) -> Self {
        Self { line, column, byte }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SourceSpan {
    pub start: SourcePosition,
    pub end: SourcePosition,
    pub bytes: Range<usize>,
}

impl SourceSpan {
    pub const fn point(position: SourcePosition) -> Self {
        Self {
            start: position,
            end: position,
            bytes: position.byte..position.byte,
        }
    }

    pub(crate) fn joined(start: &Self, end: &Self) -> Self {
        Self {
            start: start.start,
            end: end.end,
            bytes: start.bytes.start..end.bytes.end,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SpanKind {
    Document,
    Unit,
    Block,
    Field,
    MapEntry,
    Part,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SpanRecord {
    /// Stable structural path such as `units[0].targets[0].text`.
    pub path: String,
    pub kind: SpanKind,
    pub span: SourceSpan,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct SourceMap {
    entries: Vec<SpanRecord>,
    latest_by_path: HashMap<String, usize>,
}

impl SourceMap {
    pub fn entries(&self) -> &[SpanRecord] {
        &self.entries
    }

    pub fn span(&self, path: &str) -> Option<&SourceSpan> {
        self.latest_by_path
            .get(path)
            .and_then(|index| self.entries.get(*index))
            .map(|entry| &entry.span)
    }

    pub(crate) fn push(&mut self, path: String, kind: SpanKind, span: SourceSpan) {
        self.latest_by_path.insert(path.clone(), self.entries.len());
        self.entries.push(SpanRecord { path, kind, span });
    }
}

#[cfg(test)]
mod tests {
    use super::{SourceMap, SourcePosition, SourceSpan, SpanKind};

    #[test]
    fn source_map_index_tracks_the_latest_span_for_a_path() {
        let mut source_map = SourceMap::default();
        let first = SourceSpan::point(SourcePosition::new(1, 1, 0));
        let latest = SourceSpan::point(SourcePosition::new(2, 3, 5));
        source_map.push("units[0].source".to_owned(), SpanKind::Field, first);
        source_map.push(
            "units[0].source".to_owned(),
            SpanKind::Field,
            latest.clone(),
        );

        assert_eq!(source_map.span("units[0].source"), Some(&latest));
        assert!(source_map.span("units[1].source").is_none());
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ErrorCode {
    Io,
    InvalidUtf8,
    LineTooLong,
    InvalidIndentation,
    InvalidMagic,
    UnsupportedVersion,
    MissingDocument,
    MissingRequiredField,
    Duplicate,
    UnknownField,
    InvalidString,
    InvalidInteger,
    InvalidEnum,
    UnexpectedToken,
    UnclosedBlock,
    NestingLimit,
    InvalidValue,
}

impl ErrorCode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Io => "LKT001",
            Self::InvalidUtf8 => "LKT002",
            Self::LineTooLong => "LKT003",
            Self::InvalidIndentation => "LKT004",
            Self::InvalidMagic => "LKT005",
            Self::UnsupportedVersion => "LKT006",
            Self::MissingDocument => "LKT007",
            Self::MissingRequiredField => "LKT008",
            Self::Duplicate => "LKT009",
            Self::UnknownField => "LKT010",
            Self::InvalidString => "LKT011",
            Self::InvalidInteger => "LKT012",
            Self::InvalidEnum => "LKT013",
            Self::UnexpectedToken => "LKT014",
            Self::UnclosedBlock => "LKT015",
            Self::NestingLimit => "LKT016",
            Self::InvalidValue => "LKT017",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ParseError {
    pub code: ErrorCode,
    pub message: String,
    pub span: SourceSpan,
}

impl ParseError {
    pub(crate) fn new(code: ErrorCode, message: impl Into<String>, span: SourceSpan) -> Self {
        Self {
            code,
            message: message.into(),
            span,
        }
    }

    pub const fn line(&self) -> usize {
        self.span.start.line
    }

    pub const fn column(&self) -> usize {
        self.span.start.column
    }
}

impl fmt::Display for ParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} at {}:{}: {}",
            self.code.as_str(),
            self.line(),
            self.column(),
            self.message
        )
    }
}

impl Error for ParseError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiagnosticSeverity {
    Error,
    Warning,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DiagnosticCode {
    DanglingTagReference,
    UnreferencedTag,
    DuplicateTagReference,
    IncompleteTagPair,
    PartsTextMismatch,
    DuplicateLocale,
    DuplicateMapKey,
    DuplicateUnitId,
}

impl DiagnosticCode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::DanglingTagReference => "LKV001",
            Self::UnreferencedTag => "LKV002",
            Self::DuplicateTagReference => "LKV003",
            Self::IncompleteTagPair => "LKV004",
            Self::PartsTextMismatch => "LKV005",
            Self::DuplicateLocale => "LKV006",
            Self::DuplicateMapKey => "LKV007",
            Self::DuplicateUnitId => "LKV008",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Diagnostic {
    pub code: DiagnosticCode,
    pub severity: DiagnosticSeverity,
    pub message: String,
    pub path: String,
    pub span: Option<SourceSpan>,
}
