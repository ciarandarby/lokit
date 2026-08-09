use std::collections::HashSet;
use std::io::{BufRead, BufReader, Cursor, Read};
use std::str::FromStr;

use crate::diagnostic::{ErrorCode, ParseError, SourceMap, SourcePosition, SourceSpan, SpanKind};
use crate::id_registry::BoundedIdRegistry;
use crate::model::{
    AdjacentContext, BaseStructure, CodePart, Comment, Data, Meta, Origin, Plural, SegmentPart,
    Tags, TargetData, TargetTags, TextPart, TieData, TieType,
};
use crate::{MAGIC, SCHEMA_VERSION};

/// Maximum physical line size accepted by default and emitted by the
/// canonical writer.
pub const MAX_LINE_BYTES: usize = 1024 * 1024;
const DEFAULT_MAX_NESTING: usize = 16;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ParseOptions {
    pub max_line_bytes: usize,
    pub max_nesting: usize,
}

impl Default for ParseOptions {
    fn default() -> Self {
        Self {
            max_line_bytes: MAX_LINE_BYTES,
            max_nesting: DEFAULT_MAX_NESTING,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ParsedDocument {
    pub document: BaseStructure,
    pub source_map: SourceMap,
}

pub fn parse_str(source: &str) -> Result<BaseStructure, ParseError> {
    parse_str_with_options(source, ParseOptions::default())
}

pub fn parse_str_with_options(
    source: &str,
    options: ParseOptions,
) -> Result<BaseStructure, ParseError> {
    Ok(
        StreamingReader::with_options(Cursor::new(source.as_bytes()), options)?
            .finish()?
            .document,
    )
}

pub fn parse_str_with_spans(source: &str) -> Result<ParsedDocument, ParseError> {
    parse_str_with_spans_and_options(source, ParseOptions::default())
}

pub fn parse_str_with_spans_and_options(
    source: &str,
    options: ParseOptions,
) -> Result<ParsedDocument, ParseError> {
    StreamingReader::with_spans_and_options(Cursor::new(source.as_bytes()), options)?.finish()
}

pub fn parse_reader<R: Read>(reader: R) -> Result<BaseStructure, ParseError> {
    parse_reader_with_options(reader, ParseOptions::default())
}

pub fn parse_reader_with_options<R: Read>(
    reader: R,
    options: ParseOptions,
) -> Result<BaseStructure, ParseError> {
    Ok(
        StreamingReader::with_options(BufReader::new(reader), options)?
            .finish()?
            .document,
    )
}

pub fn parse_reader_with_spans<R: Read>(reader: R) -> Result<ParsedDocument, ParseError> {
    parse_reader_with_spans_and_options(reader, ParseOptions::default())
}

pub fn parse_reader_with_spans_and_options<R: Read>(
    reader: R,
    options: ParseOptions,
) -> Result<ParsedDocument, ParseError> {
    StreamingReader::with_spans_and_options(BufReader::new(reader), options)?.finish()
}

pub struct StreamingReader<R: BufRead> {
    parser: Parser<R>,
    document: BaseStructure,
    unit_ids: BoundedIdRegistry,
    unit_index: usize,
    finished: bool,
}

impl<R: BufRead> StreamingReader<R> {
    pub fn new(reader: R) -> Result<Self, ParseError> {
        Self::with_options(reader, ParseOptions::default())
    }

    pub fn with_options(reader: R, options: ParseOptions) -> Result<Self, ParseError> {
        Self::with_span_collection(reader, options, false)
    }

    /// Construct a streaming reader that records source spans. Call
    /// [`Self::take_source_map`] after each unit to keep streaming memory
    /// bounded, or retain all records for language-server use.
    pub fn with_spans(reader: R) -> Result<Self, ParseError> {
        Self::with_spans_and_options(reader, ParseOptions::default())
    }

    pub fn with_spans_and_options(reader: R, options: ParseOptions) -> Result<Self, ParseError> {
        Self::with_span_collection(reader, options, true)
    }

    fn with_span_collection(
        reader: R,
        options: ParseOptions,
        collect_spans: bool,
    ) -> Result<Self, ParseError> {
        if options.max_line_bytes == 0 {
            return Err(ParseError::new(
                ErrorCode::LineTooLong,
                "max_line_bytes must be at least 1",
                SourceSpan::point(SourcePosition::new(1, 1, 0)),
            ));
        }
        if options.max_nesting == 0 {
            return Err(ParseError::new(
                ErrorCode::NestingLimit,
                "max_nesting must be at least 1",
                SourceSpan::point(SourcePosition::new(1, 1, 0)),
            ));
        }

        let mut parser = Parser::new(reader, options, collect_spans);
        parse_magic(&mut parser)?;
        let document_line =
            parser.next_required(ErrorCode::MissingDocument, "missing document block")?;
        require_depth(&document_line, 0)?;
        let statement = parse_statement(document_line)?;
        if statement.name != "document" || !matches!(&statement.kind, StatementKind::Block) {
            return Err(ParseError::new(
                ErrorCode::MissingDocument,
                "expected `document {` after the magic line",
                statement.span,
            ));
        }
        let document = parse_document(&mut parser, &statement.span)?;
        Ok(Self {
            parser,
            document,
            unit_ids: BoundedIdRegistry::default(),
            unit_index: 0,
            finished: false,
        })
    }

    /// Parsed document metadata. Its `data` vector remains empty until `finish`.
    pub fn document_header(&self) -> &BaseStructure {
        &self.document
    }

    pub fn source_map(&self) -> &SourceMap {
        &self.parser.source_map
    }

    /// Remove and return all source records collected so far. This lets a
    /// caller validate one streamed unit with locations without retaining
    /// spans for prior units.
    pub fn take_source_map(&mut self) -> SourceMap {
        std::mem::take(&mut self.parser.source_map)
    }

    pub fn next_unit(&mut self) -> Result<Option<(String, Data)>, ParseError> {
        if self.finished {
            return Ok(None);
        }
        let Some(line) = self.parser.next_significant()? else {
            self.finished = true;
            return Ok(None);
        };
        require_depth(&line, 0)?;
        let statement = parse_statement(line)?;
        let statement_span = statement.span.clone();
        let unit_id = if statement.name == "unit" {
            statement.into_named_block()?
        } else {
            return Err(ParseError::new(
                ErrorCode::UnexpectedToken,
                "expected a top-level `unit \"id\" {` block",
                statement_span,
            ));
        };
        let inserted = self.unit_ids.insert(&unit_id).map_err(|error| {
            ParseError::new(ErrorCode::Io, error.to_string(), statement_span.clone())
        })?;
        if !inserted {
            return Err(ParseError::new(
                ErrorCode::Duplicate,
                format!("duplicate unit id {unit_id:?}"),
                statement_span,
            ));
        }
        let path = format!("units[{}]", self.unit_index);
        let data = parse_data(&mut self.parser, &path, &statement_span)?;
        self.unit_index += 1;
        Ok(Some((unit_id, data)))
    }

    pub fn finish(mut self) -> Result<ParsedDocument, ParseError> {
        while let Some(unit) = self.next_unit()? {
            self.document.data.push(unit);
        }
        Ok(ParsedDocument {
            document: self.document,
            source_map: self.parser.source_map,
        })
    }
}

struct Parser<R: BufRead> {
    lines: LineReader<R>,
    source_map: SourceMap,
    options: ParseOptions,
    collect_spans: bool,
}

impl<R: BufRead> Parser<R> {
    fn new(reader: R, options: ParseOptions, collect_spans: bool) -> Self {
        Self {
            lines: LineReader::new(reader, options.max_line_bytes),
            source_map: SourceMap::default(),
            options,
            collect_spans,
        }
    }

    fn next_significant(&mut self) -> Result<Option<LogicalLine>, ParseError> {
        loop {
            let Some(line) = self.lines.next_line()? else {
                return Ok(None);
            };

            let comment_offset = line
                .text
                .bytes()
                .position(|value| !matches!(value, b' ' | b'\t'))
                .unwrap_or(line.text.len());
            if line.text.as_bytes().get(comment_offset) == Some(&b'#') {
                continue;
            }

            let indent = line.text.bytes().take_while(|value| *value == b' ').count();
            if line.text.as_bytes().get(indent) == Some(&b'\t') {
                return Err(ParseError::new(
                    ErrorCode::InvalidIndentation,
                    "tab characters are not permitted in indentation",
                    line.point_span(indent),
                ));
            }
            if indent % 2 != 0 {
                return Err(ParseError::new(
                    ErrorCode::InvalidIndentation,
                    "indentation must use multiples of two spaces",
                    line.point_span(indent),
                ));
            }
            let depth = indent / 2;
            if depth > self.options.max_nesting {
                return Err(ParseError::new(
                    ErrorCode::NestingLimit,
                    format!(
                        "nesting depth {depth} exceeds configured limit {}",
                        self.options.max_nesting
                    ),
                    line.point_span(indent),
                ));
            }
            let content_len = line.text[indent..].trim_end_matches(' ').len();
            if content_len == 0 || line.text[indent..].starts_with('#') {
                continue;
            }
            return Ok(Some(LogicalLine {
                number: line.number,
                byte_start: line.byte_start,
                byte_end: line.byte_end,
                text: line.text,
                indent,
                depth,
                content_len,
            }));
        }
    }

    fn next_required(
        &mut self,
        code: ErrorCode,
        message: &'static str,
    ) -> Result<LogicalLine, ParseError> {
        self.next_significant()?.ok_or_else(|| {
            ParseError::new(code, message, SourceSpan::point(self.lines.eof_position()))
        })
    }

    fn next_block_statement(
        &mut self,
        child_depth: usize,
        block_name: &str,
    ) -> Result<BlockItem, ParseError> {
        let Some(line) = self.next_significant()? else {
            return Err(ParseError::new(
                ErrorCode::UnclosedBlock,
                format!("unclosed {block_name} block"),
                SourceSpan::point(self.lines.eof_position()),
            ));
        };
        if line.content() == "}" {
            if line.depth + 1 != child_depth {
                return Err(ParseError::new(
                    ErrorCode::InvalidIndentation,
                    format!("closing brace for {block_name} has the wrong indentation"),
                    line.span(),
                ));
            }
            return Ok(BlockItem::Close(line.span()));
        }
        require_depth(&line, child_depth)?;
        Ok(BlockItem::Statement(parse_statement(line)?))
    }

    fn record(&mut self, path: impl Into<String>, kind: SpanKind, span: SourceSpan) {
        if self.collect_spans {
            self.source_map.push(path.into(), kind, span);
        }
    }

    fn record_lazy(&mut self, path: impl FnOnce() -> String, kind: SpanKind, span: SourceSpan) {
        if self.collect_spans {
            self.source_map.push(path(), kind, span);
        }
    }
}

struct LineReader<R: BufRead> {
    reader: R,
    max_line_bytes: usize,
    line_number: usize,
    byte_offset: usize,
}

impl<R: BufRead> LineReader<R> {
    fn new(reader: R, max_line_bytes: usize) -> Self {
        Self {
            reader,
            max_line_bytes,
            line_number: 0,
            byte_offset: 0,
        }
    }

    fn next_line(&mut self) -> Result<Option<PhysicalLine>, ParseError> {
        let start = self.byte_offset;
        let next_number = self.line_number + 1;
        let mut bytes = Vec::new();
        let mut saw_data = false;
        loop {
            let available = self.reader.fill_buf().map_err(|error| {
                ParseError::new(
                    ErrorCode::Io,
                    error.to_string(),
                    SourceSpan::point(SourcePosition::new(next_number, 1, self.byte_offset)),
                )
            })?;
            if available.is_empty() {
                break;
            }
            saw_data = true;
            let newline = available.iter().position(|value| *value == b'\n');
            let take = newline.unwrap_or(available.len());
            if bytes.len().saturating_add(take) > self.max_line_bytes {
                let remaining = self.max_line_bytes.saturating_sub(bytes.len());
                bytes.extend_from_slice(&available[..remaining]);
                let (valid_boundary, column) = scalar_boundary_position(&bytes);
                let offset = start.saturating_add(valid_boundary);
                return Err(ParseError::new(
                    ErrorCode::LineTooLong,
                    format!("line exceeds {} bytes", self.max_line_bytes),
                    SourceSpan::point(SourcePosition::new(next_number, column, offset)),
                ));
            }
            bytes.extend_from_slice(&available[..take]);
            let consumed = take + usize::from(newline.is_some());
            self.reader.consume(consumed);
            self.byte_offset += consumed;
            if newline.is_some() {
                break;
            }
        }
        if !saw_data && bytes.is_empty() {
            return Ok(None);
        }
        self.line_number = next_number;
        if bytes.last() == Some(&b'\r') {
            bytes.pop();
        }
        let content_end = start + bytes.len();
        let text = String::from_utf8(bytes).map_err(|error| {
            let (valid_boundary, column) = scalar_boundary_position(error.as_bytes());
            ParseError::new(
                ErrorCode::InvalidUtf8,
                "input is not valid UTF-8",
                SourceSpan::point(SourcePosition::new(
                    next_number,
                    column,
                    start + valid_boundary,
                )),
            )
        })?;
        Ok(Some(PhysicalLine {
            number: next_number,
            byte_start: start,
            byte_end: content_end,
            text,
        }))
    }

    fn eof_position(&self) -> SourcePosition {
        SourcePosition::new(self.line_number + 1, 1, self.byte_offset)
    }
}

/// Return the greatest valid UTF-8 prefix boundary and its one-based Unicode
/// scalar column. For an incomplete or invalid sequence the position lands
/// immediately before that sequence rather than in the middle of it.
fn scalar_boundary_position(bytes: &[u8]) -> (usize, usize) {
    match std::str::from_utf8(bytes) {
        Ok(text) => (bytes.len(), text.chars().count() + 1),
        Err(error) => {
            let valid_boundary = error.valid_up_to();
            let scalar_count = std::str::from_utf8(&bytes[..valid_boundary])
                .map_or(0, |text| text.chars().count());
            (valid_boundary, scalar_count + 1)
        }
    }
}

struct PhysicalLine {
    number: usize,
    byte_start: usize,
    byte_end: usize,
    text: String,
}

impl PhysicalLine {
    fn point_span(&self, byte_in_line: usize) -> SourceSpan {
        let column = self.text[..byte_in_line].chars().count() + 1;
        SourceSpan::point(SourcePosition::new(
            self.number,
            column,
            self.byte_start + byte_in_line,
        ))
    }
}

#[derive(Clone)]
struct LogicalLine {
    number: usize,
    byte_start: usize,
    byte_end: usize,
    text: String,
    indent: usize,
    depth: usize,
    content_len: usize,
}

impl LogicalLine {
    fn content(&self) -> &str {
        &self.text[self.indent..self.indent + self.content_len]
    }

    fn span(&self) -> SourceSpan {
        let start =
            SourcePosition::new(self.number, self.indent + 1, self.byte_start + self.indent);
        let end_column = self.text[..self.indent + self.content_len].chars().count() + 1;
        let end = SourcePosition::new(
            self.number,
            end_column,
            self.byte_start + self.indent + self.content_len,
        );
        SourceSpan {
            start,
            end,
            bytes: start.byte..end.byte.min(self.byte_end),
        }
    }
}

enum BlockItem {
    Statement(Statement),
    Close(SourceSpan),
}

#[derive(Clone, Debug)]
struct Statement {
    name: String,
    kind: StatementKind,
    span: SourceSpan,
}

#[derive(Clone, Debug)]
enum StatementKind {
    Property(RawValue),
    NamedProperty(String, RawValue),
    Block,
    NamedBlock(String),
}

impl Statement {
    fn into_property(self) -> Result<RawValue, ParseError> {
        match self.kind {
            StatementKind::Property(value) => Ok(value),
            _ => Err(self.shape_error("property")),
        }
    }

    fn into_named_property(self) -> Result<(String, RawValue), ParseError> {
        match self.kind {
            StatementKind::NamedProperty(key, value) => Ok((key, value)),
            _ => Err(self.shape_error("named property")),
        }
    }

    fn into_block(self) -> Result<(), ParseError> {
        if matches!(self.kind, StatementKind::Block) {
            Ok(())
        } else {
            Err(self.shape_error("block"))
        }
    }

    fn into_named_block(self) -> Result<String, ParseError> {
        match self.kind {
            StatementKind::NamedBlock(key) => Ok(key),
            _ => Err(self.shape_error("named block")),
        }
    }

    fn shape_error(&self, expected: &str) -> ParseError {
        ParseError::new(
            ErrorCode::UnexpectedToken,
            format!("{} must be a {expected}", self.name),
            self.span.clone(),
        )
    }
}

#[derive(Clone, Debug)]
enum RawValue {
    String(String),
    Integer(i64),
    Identifier(String),
    StringList(Vec<String>),
}

impl RawValue {
    fn into_string(self, field: &str, span: &SourceSpan) -> Result<String, ParseError> {
        match self {
            Self::String(value) => Ok(value),
            _ => Err(invalid_value(field, "quoted string", span)),
        }
    }

    fn into_integer(self, field: &str, span: &SourceSpan) -> Result<i64, ParseError> {
        match self {
            Self::Integer(value) => Ok(value),
            _ => Err(invalid_value(field, "decimal integer", span)),
        }
    }

    fn into_identifier(self, field: &str, span: &SourceSpan) -> Result<String, ParseError> {
        match self {
            Self::Identifier(value) => Ok(value),
            _ => Err(invalid_value(field, "enum identifier", span)),
        }
    }

    fn into_string_list(self, field: &str, span: &SourceSpan) -> Result<Vec<String>, ParseError> {
        match self {
            Self::StringList(value) => Ok(value),
            _ => Err(invalid_value(field, "JSON string array", span)),
        }
    }
}

fn invalid_value(field: &str, expected: &str, span: &SourceSpan) -> ParseError {
    ParseError::new(
        ErrorCode::InvalidValue,
        format!("{field} requires a {expected}"),
        span.clone(),
    )
}

fn parse_magic<R: BufRead>(parser: &mut Parser<R>) -> Result<(), ParseError> {
    let line = parser.next_required(ErrorCode::InvalidMagic, "missing Lokit magic line")?;
    require_depth(&line, 0)?;
    if line.content() == MAGIC {
        return Ok(());
    }
    if let Some(version) = line.content().strip_prefix("@lokit ") {
        if version.parse::<u32>().ok() != Some(SCHEMA_VERSION) {
            return Err(ParseError::new(
                ErrorCode::UnsupportedVersion,
                format!("unsupported Lokit schema version {version:?}"),
                line.span(),
            ));
        }
    }
    Err(ParseError::new(
        ErrorCode::InvalidMagic,
        format!("expected magic line {MAGIC:?}"),
        line.span(),
    ))
}

fn require_depth(line: &LogicalLine, depth: usize) -> Result<(), ParseError> {
    if line.depth == depth {
        return Ok(());
    }
    Err(ParseError::new(
        ErrorCode::InvalidIndentation,
        format!(
            "expected {} spaces of indentation, found {}",
            depth * 2,
            line.indent
        ),
        line.span(),
    ))
}

fn parse_statement(line: LogicalLine) -> Result<Statement, ParseError> {
    let span = line.span();
    let mut cursor = LineCursor::new(line.content(), &line);
    let name = cursor.identifier()?;
    cursor.spaces();
    let kind = match cursor.peek() {
        Some('{') => {
            cursor.bump();
            cursor.end()?;
            StatementKind::Block
        }
        Some('=') => {
            cursor.bump();
            cursor.spaces();
            let value = cursor.value()?;
            cursor.end()?;
            StatementKind::Property(value)
        }
        Some('"') => {
            let argument = cursor.json_string()?;
            cursor.spaces();
            match cursor.peek() {
                Some('{') => {
                    cursor.bump();
                    cursor.end()?;
                    StatementKind::NamedBlock(argument)
                }
                Some('=') => {
                    cursor.bump();
                    cursor.spaces();
                    let value = cursor.value()?;
                    cursor.end()?;
                    StatementKind::NamedProperty(argument, value)
                }
                _ => return Err(cursor.error(ErrorCode::UnexpectedToken, "expected `{` or `=`")),
            }
        }
        _ => {
            return Err(cursor.error(
                ErrorCode::UnexpectedToken,
                "expected `{`, `=`, or a quoted key",
            ))
        }
    };
    Ok(Statement { name, kind, span })
}

struct LineCursor<'a> {
    source: &'a str,
    offset: usize,
    line: &'a LogicalLine,
}

impl<'a> LineCursor<'a> {
    fn new(source: &'a str, line: &'a LogicalLine) -> Self {
        Self {
            source,
            offset: 0,
            line,
        }
    }

    fn peek(&self) -> Option<char> {
        self.source[self.offset..].chars().next()
    }

    fn bump(&mut self) -> Option<char> {
        let value = self.peek()?;
        self.offset += value.len_utf8();
        Some(value)
    }

    fn spaces(&mut self) {
        while self.peek() == Some(' ') {
            self.offset += 1;
        }
    }

    fn identifier(&mut self) -> Result<String, ParseError> {
        let start = self.offset;
        let Some(first) = self.peek() else {
            return Err(self.error(ErrorCode::UnexpectedToken, "expected identifier"));
        };
        if !(first.is_ascii_alphabetic() || first == '_') {
            return Err(self.error(ErrorCode::UnexpectedToken, "expected identifier"));
        }
        self.bump();
        while let Some(value) = self.peek() {
            if value.is_ascii_alphanumeric() || matches!(value, '_' | '-' | '.') {
                self.bump();
            } else {
                break;
            }
        }
        Ok(self.source[start..self.offset].to_owned())
    }

    fn value(&mut self) -> Result<RawValue, ParseError> {
        match self.peek() {
            Some('"') => self.json_string().map(RawValue::String),
            Some('[') => self.string_list().map(RawValue::StringList),
            Some('-' | '0'..='9') => self.integer().map(RawValue::Integer),
            Some(_) => self.identifier().map(RawValue::Identifier),
            None => Err(self.error(ErrorCode::InvalidValue, "missing property value")),
        }
    }

    fn integer(&mut self) -> Result<i64, ParseError> {
        let start = self.offset;
        if self.peek() == Some('-') {
            self.bump();
        }
        let digits = self.offset;
        while matches!(self.peek(), Some('0'..='9')) {
            self.bump();
        }
        if self.offset == digits {
            return Err(self.error_at(
                ErrorCode::InvalidInteger,
                "integer requires decimal digits",
                start,
            ));
        }
        self.source[start..self.offset].parse::<i64>().map_err(|_| {
            self.error_at(
                ErrorCode::InvalidInteger,
                "integer is outside the signed 64-bit range",
                start,
            )
        })
    }

    fn string_list(&mut self) -> Result<Vec<String>, ParseError> {
        self.bump();
        self.spaces();
        let mut values = Vec::new();
        if self.peek() == Some(']') {
            self.bump();
            return Ok(values);
        }
        loop {
            if self.peek() != Some('"') {
                return Err(self.error(
                    ErrorCode::InvalidValue,
                    "string arrays may contain only quoted strings",
                ));
            }
            values.push(self.json_string()?);
            self.spaces();
            match self.peek() {
                Some(',') => {
                    self.bump();
                    self.spaces();
                }
                Some(']') => {
                    self.bump();
                    return Ok(values);
                }
                _ => {
                    return Err(self.error(
                        ErrorCode::UnexpectedToken,
                        "expected `,` or `]` in string array",
                    ))
                }
            }
        }
    }

    fn json_string(&mut self) -> Result<String, ParseError> {
        let quote_offset = self.offset;
        if self.bump() != Some('"') {
            return Err(self.error(ErrorCode::InvalidString, "expected quoted string"));
        }
        let mut output = String::new();
        loop {
            let Some(value) = self.bump() else {
                return Err(self.error_at(
                    ErrorCode::InvalidString,
                    "unterminated quoted string",
                    quote_offset,
                ));
            };
            match value {
                '"' => return Ok(output),
                '\\' => {
                    let escape_offset = self.offset.saturating_sub(1);
                    let Some(escaped) = self.bump() else {
                        return Err(self.error_at(
                            ErrorCode::InvalidString,
                            "unterminated string escape",
                            escape_offset,
                        ));
                    };
                    match escaped {
                        '"' => output.push('"'),
                        '\\' => output.push('\\'),
                        '/' => output.push('/'),
                        'b' => output.push('\u{0008}'),
                        'f' => output.push('\u{000c}'),
                        'n' => output.push('\n'),
                        'r' => output.push('\r'),
                        't' => output.push('\t'),
                        'u' => output.push(self.unicode_escape(escape_offset)?),
                        _ => {
                            return Err(self.error_at(
                                ErrorCode::InvalidString,
                                format!("invalid JSON escape `\\{escaped}`"),
                                escape_offset,
                            ))
                        }
                    }
                }
                value if value <= '\u{001f}' => {
                    return Err(self.error_at(
                        ErrorCode::InvalidString,
                        "unescaped control character in string",
                        self.offset.saturating_sub(value.len_utf8()),
                    ))
                }
                _ => output.push(value),
            }
        }
    }

    fn unicode_escape(&mut self, escape_offset: usize) -> Result<char, ParseError> {
        let high = self.hex_quad(escape_offset)?;
        let scalar = if (0xd800..=0xdbff).contains(&high) {
            if !self.source[self.offset..].starts_with("\\u") {
                return Err(self.error_at(
                    ErrorCode::InvalidString,
                    "high surrogate must be followed by a low surrogate",
                    escape_offset,
                ));
            }
            self.offset += 2;
            let low = self.hex_quad(escape_offset)?;
            if !(0xdc00..=0xdfff).contains(&low) {
                return Err(self.error_at(
                    ErrorCode::InvalidString,
                    "invalid low surrogate",
                    escape_offset,
                ));
            }
            0x1_0000 + ((u32::from(high) - 0xd800) << 10) + (u32::from(low) - 0xdc00)
        } else if (0xdc00..=0xdfff).contains(&high) {
            return Err(self.error_at(
                ErrorCode::InvalidString,
                "lone low surrogate is invalid",
                escape_offset,
            ));
        } else {
            u32::from(high)
        };
        char::from_u32(scalar).ok_or_else(|| {
            self.error_at(
                ErrorCode::InvalidString,
                "invalid Unicode scalar value",
                escape_offset,
            )
        })
    }

    fn hex_quad(&mut self, escape_offset: usize) -> Result<u16, ParseError> {
        if self.source.len().saturating_sub(self.offset) < 4 {
            return Err(self.error_at(
                ErrorCode::InvalidString,
                "Unicode escape requires four hexadecimal digits",
                escape_offset,
            ));
        }
        let digits = &self.source.as_bytes()[self.offset..self.offset + 4];
        if !digits.iter().all(u8::is_ascii_hexdigit) {
            return Err(self.error_at(
                ErrorCode::InvalidString,
                "Unicode escape contains a non-hexadecimal digit",
                escape_offset,
            ));
        }
        self.offset += 4;
        Ok(digits.iter().fold(0_u16, |result, digit| {
            let value = match digit {
                b'0'..=b'9' => digit - b'0',
                b'a'..=b'f' => digit - b'a' + 10,
                b'A'..=b'F' => digit - b'A' + 10,
                _ => unreachable!("hexadecimal digits were validated"),
            };
            (result << 4) | u16::from(value)
        }))
    }

    fn end(&mut self) -> Result<(), ParseError> {
        self.spaces();
        if self.offset == self.source.len() {
            Ok(())
        } else {
            Err(self.error(ErrorCode::UnexpectedToken, "unexpected trailing content"))
        }
    }

    fn error(&self, code: ErrorCode, message: impl Into<String>) -> ParseError {
        self.error_at(code, message, self.offset)
    }

    fn error_at(
        &self,
        code: ErrorCode,
        message: impl Into<String>,
        content_offset: usize,
    ) -> ParseError {
        let byte = self.line.byte_start + self.line.indent + content_offset;
        let column = self.line.text[..self.line.indent + content_offset]
            .chars()
            .count()
            + 1;
        ParseError::new(
            code,
            message,
            SourceSpan::point(SourcePosition::new(self.line.number, column, byte)),
        )
    }
}

fn parse_document<R: BufRead>(
    parser: &mut Parser<R>,
    start: &SourceSpan,
) -> Result<BaseStructure, ParseError> {
    let mut document = BaseStructure::new("");
    let mut seen = HashSet::new();
    let mut extension_keys = HashSet::new();
    loop {
        match parser.next_block_statement(1, "document")? {
            BlockItem::Close(close) => {
                if !seen.contains("source_locale") {
                    return Err(missing_required("document", "source_locale", &close));
                }
                parser.record(
                    "document",
                    SpanKind::Document,
                    SourceSpan::joined(start, &close),
                );
                return Ok(document);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "source_locale" => {
                        set_once(&mut seen, "source_locale", &span)?;
                        document.source_locale = statement
                            .into_property()?
                            .into_string("source_locale", &span)?;
                        parser.record("document.source_locale", SpanKind::Field, span);
                    }
                    "target_locale" => {
                        set_once(&mut seen, "target_locale", &span)?;
                        document.target_locale = Some(
                            statement
                                .into_property()?
                                .into_string("target_locale", &span)?,
                        );
                        parser.record("document.target_locale", SpanKind::Field, span);
                    }
                    "target_locales" => {
                        set_once(&mut seen, "target_locales", &span)?;
                        document.target_locales = statement
                            .into_property()?
                            .into_string_list("target_locales", &span)?;
                        parser.record("document.target_locales", SpanKind::Field, span);
                    }
                    "format_version" => {
                        set_once(&mut seen, "format_version", &span)?;
                        document.format_version = statement
                            .into_property()?
                            .into_string("format_version", &span)?;
                        parser.record("document.format_version", SpanKind::Field, span);
                    }
                    "export_origin" => {
                        set_once(&mut seen, "export_origin", &span)?;
                        document.export_origin = statement
                            .into_property()?
                            .into_string("export_origin", &span)?;
                        parser.record("document.export_origin", SpanKind::Field, span);
                    }
                    "export_timestamp" => {
                        set_once(&mut seen, "export_timestamp", &span)?;
                        document.export_timestamp = statement
                            .into_property()?
                            .into_string("export_timestamp", &span)?;
                        parser.record("document.export_timestamp", SpanKind::Field, span);
                    }
                    "source_language" => {
                        set_once(&mut seen, "source_language", &span)?;
                        document.source_language = Some(
                            statement
                                .into_property()?
                                .into_string("source_language", &span)?,
                        );
                        parser.record("document.source_language", SpanKind::Field, span);
                    }
                    "target_language" => {
                        set_once(&mut seen, "target_language", &span)?;
                        document.target_language = Some(
                            statement
                                .into_property()?
                                .into_string("target_language", &span)?,
                        );
                        parser.record("document.target_language", SpanKind::Field, span);
                    }
                    "target_languages" => {
                        set_once(&mut seen, "target_languages", &span)?;
                        document.target_languages = statement
                            .into_property()?
                            .into_string_list("target_languages", &span)?;
                        parser.record("document.target_languages", SpanKind::Field, span);
                    }
                    "extension" => push_named_string(
                        statement,
                        &mut document.extensions,
                        &mut extension_keys,
                        parser,
                        "document.extensions",
                    )?,
                    _ => return Err(unknown(&statement, "document")),
                }
            }
        }
    }
}

fn parse_data<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<Data, ParseError> {
    let mut data = Data::new("");
    let mut seen = HashSet::new();
    let mut target_locales = HashSet::new();
    let mut extension_keys = HashSet::new();
    loop {
        match parser.next_block_statement(1, "unit")? {
            BlockItem::Close(close) => {
                if !seen.contains("source") {
                    return Err(missing_required("unit", "source", &close));
                }
                parser.record(path, SpanKind::Unit, SourceSpan::joined(start, &close));
                return Ok(data);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "source" => {
                        set_once(&mut seen, "source", &span)?;
                        data.source = statement.into_property()?.into_string("source", &span)?;
                        parser.record_lazy(|| format!("{path}.source"), SpanKind::Field, span);
                    }
                    "target" if matches!(&statement.kind, StatementKind::Property(_)) => {
                        set_once(&mut seen, "target", &span)?;
                        data.target =
                            Some(statement.into_property()?.into_string("target", &span)?);
                        parser.record_lazy(|| format!("{path}.target"), SpanKind::Field, span);
                    }
                    "target" => {
                        let locale = statement.clone().into_named_block()?;
                        if !target_locales.insert(locale.clone()) {
                            return Err(duplicate("target locale", &locale, &span));
                        }
                        let index = data.targets.len();
                        let target_path = format!("{path}.targets[{index}]");
                        let target = parse_target_data(parser, &target_path, &span)?;
                        data.targets.push((locale, target));
                    }
                    "plural" => {
                        set_once(&mut seen, "plural", &span)?;
                        statement.into_block()?;
                        data.plural = Some(parse_plural(parser, &format!("{path}.plural"), &span)?);
                    }
                    "tags" => {
                        set_once(&mut seen, "tags", &span)?;
                        statement.into_block()?;
                        data.tags = Some(parse_tags(parser, &format!("{path}.tags"), &span)?);
                    }
                    "meta" => {
                        set_once(&mut seen, "meta", &span)?;
                        statement.into_block()?;
                        data.meta = parse_meta(parser, &format!("{path}.meta"), &span)?;
                    }
                    "status" => {
                        set_once(&mut seen, "status", &span)?;
                        data.status = parse_enum(statement.into_property()?, "status", &span)?;
                        parser.record_lazy(|| format!("{path}.status"), SpanKind::Field, span);
                    }
                    "comment" => {
                        statement.into_block()?;
                        let comment_path = format!("{path}.comments[{}]", data.comments.len());
                        data.comments
                            .push(parse_comment(parser, &comment_path, &span)?);
                    }
                    "previous_context" => {
                        set_once(&mut seen, "previous_context", &span)?;
                        statement.into_block()?;
                        data.previous_context = Some(parse_context(
                            parser,
                            &format!("{path}.previous_context"),
                            &span,
                        )?);
                    }
                    "next_context" => {
                        set_once(&mut seen, "next_context", &span)?;
                        statement.into_block()?;
                        data.next_context = Some(parse_context(
                            parser,
                            &format!("{path}.next_context"),
                            &span,
                        )?);
                    }
                    "extension" => push_named_string(
                        statement,
                        &mut data.extensions,
                        &mut extension_keys,
                        parser,
                        &format!("{path}.extensions"),
                    )?,
                    _ => return Err(unknown(&statement, "unit")),
                }
            }
        }
    }
}

fn parse_target_data<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<TargetData, ParseError> {
    let mut target = TargetData::default();
    let mut seen = HashSet::new();
    let mut extension_keys = HashSet::new();
    loop {
        match parser.next_block_statement(2, "target")? {
            BlockItem::Close(close) => {
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(target);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "text" => {
                        set_once(&mut seen, "text", &span)?;
                        target.text = Some(statement.into_property()?.into_string("text", &span)?);
                        parser.record_lazy(|| format!("{path}.text"), SpanKind::Field, span);
                    }
                    "status" => {
                        set_once(&mut seen, "status", &span)?;
                        target.status = parse_enum(statement.into_property()?, "status", &span)?;
                        parser.record_lazy(|| format!("{path}.status"), SpanKind::Field, span);
                    }
                    "tags" => {
                        set_once(&mut seen, "tags", &span)?;
                        statement.into_block()?;
                        target.tags =
                            Some(parse_target_tags(parser, &format!("{path}.tags"), &span)?);
                    }
                    "plural" => {
                        set_once(&mut seen, "plural", &span)?;
                        statement.into_block()?;
                        target.plural =
                            Some(parse_plural(parser, &format!("{path}.plural"), &span)?);
                    }
                    "meta" => {
                        set_once(&mut seen, "meta", &span)?;
                        statement.into_block()?;
                        target.meta = parse_meta(parser, &format!("{path}.meta"), &span)?;
                    }
                    "comment" => {
                        statement.into_block()?;
                        let comment_path = format!("{path}.comments[{}]", target.comments.len());
                        target
                            .comments
                            .push(parse_comment(parser, &comment_path, &span)?);
                    }
                    "extension" => push_named_string(
                        statement,
                        &mut target.extensions,
                        &mut extension_keys,
                        parser,
                        &format!("{path}.extensions"),
                    )?,
                    _ => return Err(unknown(&statement, "target")),
                }
            }
        }
    }
}

fn parse_plural<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<Plural, ParseError> {
    let mut plural = Plural::new("");
    let mut seen = HashSet::new();
    let mut extension_keys = HashSet::new();
    loop {
        match parser.next_block_statement(start.start.column / 2 + 1, "plural")? {
            BlockItem::Close(close) => {
                if !seen.contains("variant") {
                    return Err(missing_required("plural", "variant", &close));
                }
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(plural);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "variant" => {
                        set_once(&mut seen, "variant", &span)?;
                        plural.variant =
                            statement.into_property()?.into_string("variant", &span)?;
                        parser.record_lazy(|| format!("{path}.variant"), SpanKind::Field, span);
                    }
                    "count" => {
                        set_once(&mut seen, "count", &span)?;
                        plural.count =
                            Some(statement.into_property()?.into_integer("count", &span)?);
                        parser.record_lazy(|| format!("{path}.count"), SpanKind::Field, span);
                    }
                    "category" => {
                        set_once(&mut seen, "category", &span)?;
                        plural.category =
                            Some(parse_enum(statement.into_property()?, "category", &span)?);
                        parser.record_lazy(|| format!("{path}.category"), SpanKind::Field, span);
                    }
                    "extension" => push_named_string(
                        statement,
                        &mut plural.extensions,
                        &mut extension_keys,
                        parser,
                        &format!("{path}.extensions"),
                    )?,
                    _ => return Err(unknown(&statement, "plural")),
                }
            }
        }
    }
}

fn parse_meta<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<Meta, ParseError> {
    let mut meta = Meta::default();
    let mut seen = HashSet::new();
    let mut extension_keys = HashSet::new();
    let child_depth = start.start.column / 2 + 1;
    loop {
        match parser.next_block_statement(child_depth, "meta")? {
            BlockItem::Close(close) => {
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(meta);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "usage_count" => set_optional_integer(
                        statement,
                        &mut seen,
                        &mut meta.usage_count,
                        parser,
                        path,
                    )?,
                    "last_used" => set_optional_string(
                        statement,
                        &mut seen,
                        &mut meta.last_used,
                        parser,
                        path,
                    )?,
                    "first_used" => set_optional_string(
                        statement,
                        &mut seen,
                        &mut meta.first_used,
                        parser,
                        path,
                    )?,
                    "created" => {
                        set_optional_string(statement, &mut seen, &mut meta.created, parser, path)?
                    }
                    "updated" => {
                        set_optional_string(statement, &mut seen, &mut meta.updated, parser, path)?
                    }
                    "max_length" => set_optional_integer(
                        statement,
                        &mut seen,
                        &mut meta.max_length,
                        parser,
                        path,
                    )?,
                    "min_length" => set_optional_integer(
                        statement,
                        &mut seen,
                        &mut meta.min_length,
                        parser,
                        path,
                    )?,
                    "extension" => push_named_string(
                        statement,
                        &mut meta.extensions,
                        &mut extension_keys,
                        parser,
                        &format!("{path}.extensions"),
                    )?,
                    _ => {
                        return Err(ParseError::new(
                            ErrorCode::UnknownField,
                            format!("unknown field {:?} in meta", statement.name),
                            span,
                        ))
                    }
                }
            }
        }
    }
}

fn parse_comment<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<Comment, ParseError> {
    let mut comment = Comment::new("");
    let mut seen = HashSet::new();
    let mut extension_keys = HashSet::new();
    let child_depth = start.start.column / 2 + 1;
    loop {
        match parser.next_block_statement(child_depth, "comment")? {
            BlockItem::Close(close) => {
                if !seen.contains("context") {
                    return Err(missing_required("comment", "context", &close));
                }
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(comment);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "context" => {
                        set_once(&mut seen, "context", &span)?;
                        comment.context =
                            statement.into_property()?.into_string("context", &span)?;
                        parser.record_lazy(|| format!("{path}.context"), SpanKind::Field, span);
                    }
                    "timestamp" => set_optional_string(
                        statement,
                        &mut seen,
                        &mut comment.timestamp,
                        parser,
                        path,
                    )?,
                    "context_key" => set_optional_string(
                        statement,
                        &mut seen,
                        &mut comment.context_key,
                        parser,
                        path,
                    )?,
                    "origin" => {
                        set_once(&mut seen, "origin", &span)?;
                        statement.into_block()?;
                        comment.origin =
                            Some(parse_origin(parser, &format!("{path}.origin"), &span)?);
                    }
                    "extension" => push_named_string(
                        statement,
                        &mut comment.extensions,
                        &mut extension_keys,
                        parser,
                        &format!("{path}.extensions"),
                    )?,
                    _ => return Err(unknown(&statement, "comment")),
                }
            }
        }
    }
}

fn parse_origin<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<Origin, ParseError> {
    let mut origin = Origin::default();
    let mut seen = HashSet::new();
    let mut extension_keys = HashSet::new();
    let child_depth = start.start.column / 2 + 1;
    loop {
        match parser.next_block_statement(child_depth, "origin")? {
            BlockItem::Close(close) => {
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(origin);
            }
            BlockItem::Statement(statement) => match statement.name.as_str() {
                "system" => {
                    set_optional_string(statement, &mut seen, &mut origin.system, parser, path)?
                }
                "project" => {
                    set_optional_string(statement, &mut seen, &mut origin.project, parser, path)?
                }
                "creator_id" => {
                    set_optional_string(statement, &mut seen, &mut origin.creator_id, parser, path)?
                }
                "extension" => push_named_string(
                    statement,
                    &mut origin.extensions,
                    &mut extension_keys,
                    parser,
                    &format!("{path}.extensions"),
                )?,
                _ => return Err(unknown(&statement, "origin")),
            },
        }
    }
}

fn parse_context<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<AdjacentContext, ParseError> {
    let mut context = AdjacentContext::default();
    let mut seen = HashSet::new();
    let mut extension_keys = HashSet::new();
    let child_depth = start.start.column / 2 + 1;
    loop {
        match parser.next_block_statement(child_depth, "context")? {
            BlockItem::Close(close) => {
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(context);
            }
            BlockItem::Statement(statement) => match statement.name.as_str() {
                "unit_id" => {
                    set_optional_string(statement, &mut seen, &mut context.unit_id, parser, path)?
                }
                "source" => {
                    set_optional_string(statement, &mut seen, &mut context.source, parser, path)?
                }
                "target" => {
                    set_optional_string(statement, &mut seen, &mut context.target, parser, path)?
                }
                "extension" => push_named_string(
                    statement,
                    &mut context.extensions,
                    &mut extension_keys,
                    parser,
                    &format!("{path}.extensions"),
                )?,
                _ => return Err(unknown(&statement, "context")),
            },
        }
    }
}

fn parse_tags<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<Tags, ParseError> {
    let mut tags = Tags::default();
    let mut seen = HashSet::new();
    let mut source_keys = HashSet::new();
    let mut target_keys = HashSet::new();
    let child_depth = start.start.column / 2 + 1;
    loop {
        match parser.next_block_statement(child_depth, "tags")? {
            BlockItem::Close(close) => {
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(tags);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "source_tag" => {
                        let key = statement.clone().into_named_block()?;
                        if !source_keys.insert(key.clone()) {
                            return Err(duplicate("source tag key", &key, &span));
                        }
                        let index = tags.source_tag_map.len();
                        let tag_path = format!("{path}.source_tag_map[{index}]");
                        let tag = parse_tie(parser, &tag_path, &span, child_depth + 1)?;
                        tags.source_tag_map.push((key, tag));
                    }
                    "target_tag" => {
                        let key = statement.clone().into_named_block()?;
                        if !target_keys.insert(key.clone()) {
                            return Err(duplicate("target tag key", &key, &span));
                        }
                        let index = tags.target_tag_map.len();
                        let tag_path = format!("{path}.target_tag_map[{index}]");
                        let tag = parse_tie(parser, &tag_path, &span, child_depth + 1)?;
                        tags.target_tag_map.push((key, tag));
                    }
                    "source_parts" => {
                        set_once(&mut seen, "source_parts", &span)?;
                        statement.into_block()?;
                        tags.source_parts = parse_parts(
                            parser,
                            &format!("{path}.source_parts"),
                            &span,
                            child_depth + 1,
                        )?;
                    }
                    "target_parts" => {
                        set_once(&mut seen, "target_parts", &span)?;
                        statement.into_block()?;
                        tags.target_parts = parse_parts(
                            parser,
                            &format!("{path}.target_parts"),
                            &span,
                            child_depth + 1,
                        )?;
                    }
                    _ => return Err(unknown(&statement, "tags")),
                }
            }
        }
    }
}

fn parse_target_tags<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
) -> Result<TargetTags, ParseError> {
    let mut tags = TargetTags::default();
    let mut seen = HashSet::new();
    let mut tag_keys = HashSet::new();
    let child_depth = start.start.column / 2 + 1;
    loop {
        match parser.next_block_statement(child_depth, "target tags")? {
            BlockItem::Close(close) => {
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(tags);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "tag" => {
                        let key = statement.clone().into_named_block()?;
                        if !tag_keys.insert(key.clone()) {
                            return Err(duplicate("tag key", &key, &span));
                        }
                        let index = tags.tag_map.len();
                        let tag_path = format!("{path}.tag_map[{index}]");
                        let tag = parse_tie(parser, &tag_path, &span, child_depth + 1)?;
                        tags.tag_map.push((key, tag));
                    }
                    "parts" => {
                        set_once(&mut seen, "parts", &span)?;
                        statement.into_block()?;
                        tags.parts =
                            parse_parts(parser, &format!("{path}.parts"), &span, child_depth + 1)?;
                    }
                    _ => return Err(unknown(&statement, "target tags")),
                }
            }
        }
    }
}

fn parse_tie<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
    child_depth: usize,
) -> Result<TieData, ParseError> {
    let mut id = None;
    let mut tie_type = None;
    let mut attributes = Vec::new();
    let mut attribute_data = String::new();
    let mut position = 0;
    let mut order = 0;
    let mut pair_id = None;
    let mut original_name = None;
    let mut original_text = None;
    let mut seen = HashSet::new();
    let mut attribute_keys = HashSet::new();
    loop {
        match parser.next_block_statement(child_depth, "tag")? {
            BlockItem::Close(close) => {
                if id.is_none() {
                    return Err(missing_required("tag", "id", &close));
                }
                if tie_type.is_none() {
                    return Err(missing_required("tag", "type", &close));
                }
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(TieData {
                    id: id.unwrap_or_default(),
                    r#type: tie_type.unwrap_or(TieType::CustomStandalone),
                    attributes,
                    attribute_data,
                    position,
                    order,
                    pair_id,
                    original_name,
                    original_text,
                });
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                match statement.name.as_str() {
                    "id" => {
                        set_once(&mut seen, "id", &span)?;
                        id = Some(statement.into_property()?.into_string("id", &span)?);
                        parser.record_lazy(|| format!("{path}.id"), SpanKind::Field, span);
                    }
                    "type" => {
                        set_once(&mut seen, "type", &span)?;
                        tie_type = Some(parse_enum(statement.into_property()?, "type", &span)?);
                        parser.record_lazy(|| format!("{path}.type"), SpanKind::Field, span);
                    }
                    "attribute" => push_named_string(
                        statement,
                        &mut attributes,
                        &mut attribute_keys,
                        parser,
                        &format!("{path}.attributes"),
                    )?,
                    "attribute_data" => {
                        set_once(&mut seen, "attribute_data", &span)?;
                        attribute_data = statement
                            .into_property()?
                            .into_string("attribute_data", &span)?;
                        parser.record_lazy(
                            || format!("{path}.attribute_data"),
                            SpanKind::Field,
                            span,
                        );
                    }
                    "position" => {
                        set_once(&mut seen, "position", &span)?;
                        position = statement.into_property()?.into_integer("position", &span)?;
                        parser.record_lazy(|| format!("{path}.position"), SpanKind::Field, span);
                    }
                    "order" => {
                        set_once(&mut seen, "order", &span)?;
                        order = statement.into_property()?.into_integer("order", &span)?;
                        parser.record_lazy(|| format!("{path}.order"), SpanKind::Field, span);
                    }
                    "pair_id" => {
                        set_optional_string(statement, &mut seen, &mut pair_id, parser, path)?
                    }
                    "original_name" => {
                        set_optional_string(statement, &mut seen, &mut original_name, parser, path)?
                    }
                    "original_text" => {
                        set_optional_string(statement, &mut seen, &mut original_text, parser, path)?
                    }
                    _ => return Err(unknown(&statement, "tag")),
                }
            }
        }
    }
}

fn parse_parts<R: BufRead>(
    parser: &mut Parser<R>,
    path: &str,
    start: &SourceSpan,
    child_depth: usize,
) -> Result<Vec<SegmentPart>, ParseError> {
    let mut parts = Vec::new();
    loop {
        match parser.next_block_statement(child_depth, "parts")? {
            BlockItem::Close(close) => {
                parser.record(path, SpanKind::Block, SourceSpan::joined(start, &close));
                return Ok(parts);
            }
            BlockItem::Statement(statement) => {
                let span = statement.span.clone();
                let value = statement
                    .clone()
                    .into_property()?
                    .into_string(&statement.name, &span)?;
                let part = match statement.name.as_str() {
                    "text" => SegmentPart::Text(TextPart::new(value)),
                    "code" => SegmentPart::Code(CodePart::new(value)),
                    _ => return Err(unknown(&statement, "parts")),
                };
                parser.record_lazy(|| format!("{path}[{}]", parts.len()), SpanKind::Part, span);
                parts.push(part);
            }
        }
    }
}

fn set_optional_string<R: BufRead>(
    statement: Statement,
    seen: &mut HashSet<String>,
    destination: &mut Option<String>,
    parser: &mut Parser<R>,
    path: &str,
) -> Result<(), ParseError> {
    let name = statement.name.clone();
    let span = statement.span.clone();
    set_once(seen, &name, &span)?;
    *destination = Some(statement.into_property()?.into_string(&name, &span)?);
    parser.record_lazy(|| format!("{path}.{name}"), SpanKind::Field, span);
    Ok(())
}

fn set_optional_integer<R: BufRead>(
    statement: Statement,
    seen: &mut HashSet<String>,
    destination: &mut Option<i64>,
    parser: &mut Parser<R>,
    path: &str,
) -> Result<(), ParseError> {
    let name = statement.name.clone();
    let span = statement.span.clone();
    set_once(seen, &name, &span)?;
    *destination = Some(statement.into_property()?.into_integer(&name, &span)?);
    parser.record_lazy(|| format!("{path}.{name}"), SpanKind::Field, span);
    Ok(())
}

fn push_named_string<R: BufRead>(
    statement: Statement,
    destination: &mut Vec<(String, String)>,
    keys: &mut HashSet<String>,
    parser: &mut Parser<R>,
    path: &str,
) -> Result<(), ParseError> {
    let span = statement.span.clone();
    let (key, raw_value) = statement.into_named_property()?;
    if !keys.insert(key.clone()) {
        return Err(duplicate("map key", &key, &span));
    }
    let value = raw_value.into_string("map value", &span)?;
    parser.record_lazy(
        || format!("{path}[{}]", destination.len()),
        SpanKind::MapEntry,
        span,
    );
    destination.push((key, value));
    Ok(())
}

fn parse_enum<T: FromStr<Err = ()>>(
    value: RawValue,
    field: &str,
    span: &SourceSpan,
) -> Result<T, ParseError> {
    let identifier = value.into_identifier(field, span)?;
    identifier.parse::<T>().map_err(|()| {
        ParseError::new(
            ErrorCode::InvalidEnum,
            format!("invalid {field} value {identifier:?}"),
            span.clone(),
        )
    })
}

fn set_once(seen: &mut HashSet<String>, field: &str, span: &SourceSpan) -> Result<(), ParseError> {
    if seen.insert(field.to_owned()) {
        Ok(())
    } else {
        Err(duplicate("field", field, span))
    }
}

fn duplicate(kind: &str, value: &str, span: &SourceSpan) -> ParseError {
    ParseError::new(
        ErrorCode::Duplicate,
        format!("duplicate {kind} {value:?}"),
        span.clone(),
    )
}

fn missing_required(block: &str, field: &str, span: &SourceSpan) -> ParseError {
    ParseError::new(
        ErrorCode::MissingRequiredField,
        format!("{block} block is missing required field {field:?}"),
        span.clone(),
    )
}

fn unknown(statement: &Statement, block: &str) -> ParseError {
    ParseError::new(
        ErrorCode::UnknownField,
        format!("unknown field or block {:?} in {block}", statement.name),
        statement.span.clone(),
    )
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::io::{Cursor, ErrorKind};

    use super::StreamingReader;
    use crate::id_registry::BoundedIdRegistry;
    use crate::ErrorCode;

    const DUPLICATE_SOURCE: &str = r#"@lokit 1
document {
  source_locale = "en"
}

unit "alpha" {
  source = "one"
}

unit "beta" {
  source = "two"
}

unit "alpha" {
  source = "duplicate"
}
"#;

    const IO_ERROR_SOURCE: &str = r#"@lokit 1
document {
  source_locale = "en"
}

unit "alpha" {
  source = "one"
}

unit "beta" {
  source = "two"
}

unit "gamma" {
  source = "three"
}
"#;

    #[test]
    fn streaming_reader_spills_and_preserves_duplicate_errors() {
        let mut reader = StreamingReader::new(Cursor::new(DUPLICATE_SOURCE.as_bytes()))
            .expect("reader should parse the header");
        reader.unit_ids = BoundedIdRegistry::with_limits(1, 8);

        let first = reader
            .next_unit()
            .expect("first unit should parse")
            .expect("first unit should exist");
        assert_eq!(first.0, "alpha");
        let second = reader
            .next_unit()
            .expect("second unit should parse")
            .expect("second unit should exist");
        assert_eq!(second.0, "beta");
        assert!(reader.unit_ids.is_spilled());

        let directory = reader
            .unit_ids
            .temporary_directory()
            .expect("spilled registry should own a directory")
            .to_owned();
        let error = reader
            .next_unit()
            .expect_err("duplicate unit ID should fail");
        assert_eq!(error.code, ErrorCode::Duplicate);
        assert_eq!(error.message, "duplicate unit id \"alpha\"");
        assert_eq!(error.line(), 14);
        assert!(directory.is_dir());

        drop(reader);
        assert!(!directory.exists());
        assert!(fs::metadata(directory).is_err());
    }

    #[test]
    fn streaming_reader_maps_registry_io_errors_and_cleans_up() {
        let mut reader = StreamingReader::new(Cursor::new(IO_ERROR_SOURCE.as_bytes()))
            .expect("reader should parse the header");
        reader.unit_ids = BoundedIdRegistry::with_limits(1, 8);
        reader.next_unit().expect("first unit should parse");
        reader.next_unit().expect("second unit should parse");

        let directory = reader
            .unit_ids
            .temporary_directory()
            .expect("spilled registry should own a directory")
            .to_owned();
        reader
            .unit_ids
            .fail_next_operation(ErrorKind::PermissionDenied);
        let error = reader
            .next_unit()
            .expect_err("registry I/O failure should stop parsing");
        assert_eq!(error.code, ErrorCode::Io);
        assert_eq!(error.message, "injected unit ID registry failure");
        assert_eq!(error.line(), 14);
        assert!(directory.is_dir());

        drop(reader);
        assert!(!directory.exists());
        assert!(fs::metadata(directory).is_err());
    }
}
