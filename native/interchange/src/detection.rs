use std::fs::File;
use std::io::{self, BufRead, BufReader, Cursor, Seek};

use pyo3::prelude::*;
use quick_xml::events::Event;

const MAX_DEPTH: usize = 128;
const CAPTURE_BYTES: usize = 256;
const MAX_LINE_BYTES: usize = 1024 * 1024;
const MAX_XML_PROBE_BYTES: u64 = 64 * 1024 * 1024;

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "Invalid JSON structural probe")
}

#[derive(Debug, PartialEq)]
enum Token {
    Mark(u8),
    String(Option<String>),
    Primitive,
}

struct JsonProbe<R> {
    input: R,
}

impl<R: BufRead> JsonProbe<R> {
    fn peek(&mut self) -> io::Result<Option<u8>> {
        Ok(self.input.fill_buf()?.first().copied())
    }

    fn take(&mut self) -> io::Result<u8> {
        let byte = self.peek()?.ok_or_else(invalid)?;
        self.input.consume(1);
        Ok(byte)
    }

    fn token(&mut self) -> io::Result<Token> {
        while self.peek()?.is_some_and(|byte| byte.is_ascii_whitespace()) {
            self.take()?;
        }
        match self.take()? {
            byte @ (b'{' | b'}' | b'[' | b']' | b':' | b',') => Ok(Token::Mark(byte)),
            b'"' => self.string(),
            b'-' | b'0'..=b'9' | b't' | b'f' | b'n' => {
                while self
                    .peek()?
                    .is_some_and(|byte| !byte.is_ascii_whitespace() && !b"{}[]:,".contains(&byte))
                {
                    self.take()?;
                }
                Ok(Token::Primitive)
            }
            _ => Err(invalid()),
        }
    }

    fn string(&mut self) -> io::Result<Token> {
        let mut captured = [0u8; CAPTURE_BYTES];
        captured[0] = b'"';
        let mut length = 1usize;
        let mut escaped = false;
        let mut hex_left = 0;
        loop {
            let byte = self.take()?;
            if length < captured.len() {
                captured[length] = byte;
            }
            length = length.saturating_add(1);
            if hex_left > 0 {
                if !byte.is_ascii_hexdigit() {
                    return Err(invalid());
                }
                hex_left -= 1;
            } else if escaped {
                if byte == b'u' {
                    hex_left = 4;
                } else if !b"\"\\/bfnrt".contains(&byte) {
                    return Err(invalid());
                }
                escaped = false;
            } else if byte == b'"' {
                break;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte < 0x20 {
                return Err(invalid());
            }
        }
        if length > captured.len() {
            return Ok(Token::String(None));
        }
        serde_json::from_slice(&captured[..length])
            .map(|value| Token::String(Some(value)))
            .map_err(|_| invalid())
    }

    fn expect(&mut self, byte: u8) -> io::Result<()> {
        if self.token()? != Token::Mark(byte) {
            return Err(invalid());
        }
        Ok(())
    }

    fn skip(&mut self, first: Token, depth: usize) -> io::Result<()> {
        if depth > MAX_DEPTH {
            return Err(invalid());
        }
        match first {
            Token::String(_) | Token::Primitive => Ok(()),
            Token::Mark(start @ (b'{' | b'[')) => {
                let end = if start == b'{' { b'}' } else { b']' };
                let mut token = self.token()?;
                if token == Token::Mark(end) {
                    return Ok(());
                }
                loop {
                    if start == b'{' {
                        if !matches!(token, Token::String(_)) {
                            return Err(invalid());
                        }
                        self.expect(b':')?;
                        token = self.token()?;
                    }
                    self.skip(token, depth + 1)?;
                    match self.token()? {
                        Token::Mark(byte) if byte == end => return Ok(()),
                        Token::Mark(b',') => token = self.token()?,
                        _ => return Err(invalid()),
                    }
                }
            }
            _ => Err(invalid()),
        }
    }

    fn unit(&mut self, first: Token, consume_all: bool) -> io::Result<bool> {
        if first != Token::Mark(b'{') {
            self.skip(first, 2)?;
            return Ok(false);
        }
        let mut source = false;
        let mut token = self.token()?;
        if token == Token::Mark(b'}') {
            return Ok(false);
        }
        loop {
            let Token::String(key) = token else {
                return Err(invalid());
            };
            self.expect(b':')?;
            let value = self.token()?;
            if key.as_deref() == Some("source") {
                source = matches!(value, Token::String(_));
                if source && !consume_all {
                    return Ok(true);
                }
            }
            self.skip(value, 3)?;
            match self.token()? {
                Token::Mark(b'}') => return Ok(source),
                Token::Mark(b',') => token = self.token()?,
                _ => return Err(invalid()),
            }
        }
    }

    fn data(&mut self, first: Token, consume_all: bool) -> io::Result<bool> {
        if first != Token::Mark(b'{') {
            self.skip(first, 1)?;
            return Ok(false);
        }
        let token = self.token()?;
        if token == Token::Mark(b'}') {
            return Ok(true);
        }
        if !matches!(token, Token::String(_)) {
            return Err(invalid());
        }
        self.expect(b':')?;
        let value = self.token()?;
        let found = self.unit(value, consume_all)?;
        if !consume_all {
            return Ok(found);
        }
        loop {
            match self.token()? {
                Token::Mark(b'}') => return Ok(found),
                Token::Mark(b',') => {
                    if !matches!(self.token()?, Token::String(_)) {
                        return Err(invalid());
                    }
                    self.expect(b':')?;
                    let value = self.token()?;
                    self.skip(value, 2)?;
                }
                _ => return Err(invalid()),
            }
        }
    }

    fn lokit(&mut self) -> io::Result<bool> {
        self.expect(b'{')?;
        let mut source = false;
        let mut data = None;
        let mut token = self.token()?;
        if token == Token::Mark(b'}') {
            return Ok(false);
        }
        loop {
            let Token::String(key) = token else {
                return Err(invalid());
            };
            self.expect(b':')?;
            let value = self.token()?;
            match key.as_deref() {
                Some("source_locale") => {
                    source = matches!(value, Token::String(_));
                    self.skip(value, 1)?;
                    if let Some(data) = data {
                        return Ok(source && data);
                    }
                }
                Some("data") => {
                    let shape = self.data(value, !source)?;
                    if source {
                        return Ok(shape);
                    }
                    data = Some(shape);
                }
                _ => self.skip(value, 1)?,
            }
            match self.token()? {
                Token::Mark(b'}') => return Ok(source && data == Some(true)),
                Token::Mark(b',') => token = self.token()?,
                _ => return Err(invalid()),
            }
        }
    }
}

fn skip_bom<R: BufRead>(input: &mut R) -> io::Result<()> {
    if input.fill_buf()?.starts_with(&[0xef, 0xbb, 0xbf]) {
        input.consume(3);
    }
    Ok(())
}

fn lokit_magic<R: BufRead>(input: &mut R) -> io::Result<bool> {
    let mut line_bytes = 0usize;
    let mut prefix = [0u8; 6];
    let mut length = 0;
    let mut comment = false;
    let mut leading = true;
    loop {
        let bytes = input.fill_buf()?;
        if bytes.is_empty() {
            return Ok(!comment && length == 6 && &prefix == b"@lokit");
        }
        let mut consumed = 0;
        for &byte in bytes {
            consumed += 1;
            if byte == b'\n' {
                if !comment && !leading {
                    return Ok(length == 6 && &prefix == b"@lokit");
                }
                line_bytes = 0;
                length = 0;
                comment = false;
                leading = true;
                continue;
            }
            line_bytes += 1;
            if line_bytes > MAX_LINE_BYTES {
                return Ok(false);
            }
            if leading && byte == b'#' {
                comment = true;
            }
            leading &= matches!(byte, b' ' | b'\t' | b'\r');
            if length < prefix.len() {
                prefix[length] = byte;
                length += 1;
            }
            if !comment && !leading && length == 6 {
                return Ok(&prefix == b"@lokit");
            }
        }
        input.consume(consumed);
    }
}

fn xml_format<R: BufRead>(input: R, fragments: bool) -> Option<&'static str> {
    let mut reader = quick_xml::Reader::from_reader(input.take(MAX_XML_PROBE_BYTES));
    reader.config_mut().enable_all_checks(true);
    let mut buffer = Vec::new();
    loop {
        buffer.clear();
        match reader.read_event_into(&mut buffer).ok()? {
            Event::Start(element) | Event::Empty(element) => {
                return match element.local_name().as_ref() {
                    name if name.eq_ignore_ascii_case(b"tmx") => Some("tmx"),
                    name if name.eq_ignore_ascii_case(b"xliff") => Some("xliff"),
                    name if name.eq_ignore_ascii_case(b"html") => Some("html"),
                    name if fragments
                        && [b"head".as_slice(), b"body", b"p", b"div"]
                            .iter()
                            .any(|tag| name.eq_ignore_ascii_case(tag)) =>
                    {
                        Some("html")
                    }
                    _ => None,
                };
            }
            Event::Eof => return None,
            _ => (),
        }
    }
}

fn detect<R: BufRead + Seek>(
    input: &mut R,
    json_hint: bool,
    bytes: bool,
) -> io::Result<Option<&'static str>> {
    skip_bom(input)?;
    if lokit_magic(input)? {
        return Ok(Some("lokit"));
    }
    input.rewind()?;
    skip_bom(input)?;
    while input
        .fill_buf()?
        .first()
        .is_some_and(u8::is_ascii_whitespace)
    {
        input.consume(1);
    }
    let mut prefix = [0u8; 1000];
    let mut length = 0;
    while length < prefix.len() {
        let read = input.read(&mut prefix[length..])?;
        if read == 0 {
            break;
        }
        length += read;
    }
    let prefix = &prefix[..length];
    input.rewind()?;
    skip_bom(input)?;
    if json_hint || prefix.starts_with(b"{") {
        return Ok(Some(if (JsonProbe { input }).lokit().unwrap_or(false) {
            "lokit_json"
        } else {
            "json_i18n"
        }));
    }
    if prefix.starts_with(b"PK\x03\x04") {
        return Ok(Some("zip"));
    }
    if prefix.starts_with(b"<") {
        if let Some(format) = xml_format(input, bytes) {
            return Ok(Some(format));
        }
        if bytes {
            let lowered = prefix.to_ascii_lowercase();
            if lowered.windows(14).any(|part| part == b"<!doctype html")
                || lowered.windows(5).any(|part| part == b"<html")
            {
                return Ok(Some("html"));
            }
        }
    }
    if bytes {
        if prefix.windows(5).any(|part| part == b"msgid") {
            return Ok(Some("po"));
        }
        if prefix.iter().any(|byte| b",;\t".contains(byte)) {
            return Ok(Some("csv"));
        }
    }
    Ok(None)
}

#[pyfunction]
#[pyo3(signature = (path, json_hint=false))]
fn detect_text_path(py: Python<'_>, path: &str, json_hint: bool) -> PyResult<Option<&'static str>> {
    Ok(py
        .detach(|| {
            let mut input = BufReader::with_capacity(8192, File::open(path)?);
            detect(&mut input, json_hint, false)
        })
        .unwrap_or(if json_hint { Some("json_i18n") } else { None }))
}

#[pyfunction]
fn detect_text_bytes(py: Python<'_>, data: &[u8]) -> PyResult<Option<&'static str>> {
    Ok(py.detach(|| detect(&mut Cursor::new(data), false, true))?)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(detect_text_path, module)?)?;
    module.add_function(wrap_pyfunction!(detect_text_bytes, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_probe_stops_before_large_target() {
        let mut source =
            Cursor::new(br#"{"source_locale":"en","data":{"u":{"source":"A","target":""#);
        assert!(JsonProbe { input: &mut source }.lokit().unwrap());
        assert!(source.position() < source.get_ref().len() as u64);
    }

    #[test]
    fn json_probe_skips_large_strings_without_retaining_them() {
        let input = format!(
            r#"{{"ignored":"{}","data":{{"u":{{"source":"A"}}}},"source_locale":"en"}}"#,
            "x".repeat(2_000_000)
        );
        assert!(JsonProbe {
            input: Cursor::new(input.as_bytes())
        }
        .lokit()
        .unwrap());
    }

    #[test]
    fn json_probe_rejects_excessive_depth() {
        let input = format!(
            r#"{{"other":{}0{},"source_locale":"en","data":{{}}}}"#,
            "[".repeat(129),
            "]".repeat(129)
        );
        assert!(JsonProbe {
            input: Cursor::new(input.as_bytes())
        }
        .lokit()
        .is_err());
    }

    #[test]
    fn magic_skips_comments_with_bounded_buffers() {
        for input in [b"# comment\n@lokit 1".as_slice(), b"\t#comment\n\n@lokit 1"] {
            assert!(lokit_magic(&mut Cursor::new(input)).unwrap());
        }
        assert!(!lokit_magic(&mut Cursor::new(b"@other")).unwrap());
    }
}
