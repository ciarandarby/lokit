use std::sync::atomic::{AtomicBool, Ordering};

#[cfg(test)]
use tower_lsp_server::ls_types::SemanticTokensClientCapabilitiesRequests;
use tower_lsp_server::ls_types::{
    Range, SemanticToken, SemanticTokenType, SemanticTokensClientCapabilities,
    SemanticTokensLegend, TokenFormat,
};

use crate::document::{LineIndex, PositionEncoding};

const BLOCK_NAMES: &[&str] = &[
    "document",
    "unit",
    "target",
    "plural",
    "meta",
    "comment",
    "origin",
    "previous_context",
    "next_context",
    "tags",
    "source_parts",
    "target_parts",
    "parts",
    "source_tag",
    "target_tag",
    "tag",
];
const ENUM_VALUES: &[&str] = &[
    "new",
    "draft",
    "translated",
    "reviewed",
    "approved",
    "rejected",
    "unknown",
    "generic",
    "zero",
    "one",
    "two",
    "few",
    "many",
    "other",
    "a.open",
    "a.close",
    "abbr.open",
    "abbr.close",
    "b.open",
    "b.close",
    "bdi.open",
    "bdi.close",
    "bdo.open",
    "bdo.close",
    "br.standalone",
    "cite.open",
    "cite.close",
    "code.open",
    "code.close",
    "data.open",
    "data.close",
    "dfn.open",
    "dfn.close",
    "em.open",
    "em.close",
    "i.open",
    "i.close",
    "img.standalone",
    "kbd.open",
    "kbd.close",
    "mark.open",
    "mark.close",
    "q.open",
    "q.close",
    "rp.open",
    "rp.close",
    "rt.open",
    "rt.close",
    "ruby.open",
    "ruby.close",
    "s.open",
    "s.close",
    "samp.open",
    "samp.close",
    "small.open",
    "small.close",
    "span.open",
    "span.close",
    "strong.open",
    "strong.close",
    "sub.open",
    "sub.close",
    "sup.open",
    "sup.close",
    "time.open",
    "time.close",
    "u.open",
    "u.close",
    "var.open",
    "var.close",
    "wbr.standalone",
    "custom.open",
    "custom.close",
    "custom.standalone",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SyntaxClass {
    Keyword,
    Property,
    String,
    Number,
    EnumMember,
    Comment,
    Operator,
}

impl SyntaxClass {
    const ALL: [Self; 7] = [
        Self::Keyword,
        Self::Property,
        Self::String,
        Self::Number,
        Self::EnumMember,
        Self::Comment,
        Self::Operator,
    ];

    const fn index(self) -> usize {
        match self {
            Self::Keyword => 0,
            Self::Property => 1,
            Self::String => 2,
            Self::Number => 3,
            Self::EnumMember => 4,
            Self::Comment => 5,
            Self::Operator => 6,
        }
    }

    fn candidates(self) -> [SemanticTokenType; 3] {
        match self {
            Self::Keyword => [
                SemanticTokenType::KEYWORD,
                SemanticTokenType::MACRO,
                SemanticTokenType::VARIABLE,
            ],
            Self::Property => [
                SemanticTokenType::PROPERTY,
                SemanticTokenType::VARIABLE,
                SemanticTokenType::KEYWORD,
            ],
            Self::String => [
                SemanticTokenType::STRING,
                SemanticTokenType::VARIABLE,
                SemanticTokenType::PROPERTY,
            ],
            Self::Number => [
                SemanticTokenType::NUMBER,
                SemanticTokenType::VARIABLE,
                SemanticTokenType::STRING,
            ],
            Self::EnumMember => [
                SemanticTokenType::ENUM_MEMBER,
                SemanticTokenType::ENUM,
                SemanticTokenType::VARIABLE,
            ],
            Self::Comment => [
                SemanticTokenType::COMMENT,
                SemanticTokenType::STRING,
                SemanticTokenType::VARIABLE,
            ],
            Self::Operator => [
                SemanticTokenType::OPERATOR,
                SemanticTokenType::KEYWORD,
                SemanticTokenType::VARIABLE,
            ],
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct SemanticLegend {
    legend: SemanticTokensLegend,
    token_indices: [u32; SyntaxClass::ALL.len()],
}

impl SemanticLegend {
    pub(crate) fn negotiate(capabilities: &SemanticTokensClientCapabilities) -> Option<Self> {
        if !capabilities.formats.contains(&TokenFormat::RELATIVE)
            || capabilities.token_types.is_empty()
        {
            return None;
        }

        let mut token_types = Vec::new();
        let mut token_indices = [0_u32; SyntaxClass::ALL.len()];
        for syntax_class in SyntaxClass::ALL {
            let selected = syntax_class
                .candidates()
                .into_iter()
                .find(|candidate| capabilities.token_types.contains(candidate))
                .unwrap_or_else(|| capabilities.token_types[0].clone());
            let index = token_types
                .iter()
                .position(|candidate| candidate == &selected)
                .unwrap_or_else(|| {
                    token_types.push(selected);
                    token_types.len() - 1
                });
            token_indices[syntax_class.index()] = u32::try_from(index).unwrap_or(u32::MAX);
        }
        Some(Self {
            legend: SemanticTokensLegend {
                token_types,
                token_modifiers: Vec::new(),
            },
            token_indices,
        })
    }

    pub(crate) fn lsp(&self) -> SemanticTokensLegend {
        self.legend.clone()
    }

    const fn token_index(&self, syntax_class: SyntaxClass) -> u32 {
        self.token_indices[syntax_class.index()]
    }

    #[cfg(test)]
    fn canonical() -> Self {
        Self {
            legend: SemanticTokensLegend {
                token_types: vec![
                    SemanticTokenType::KEYWORD,
                    SemanticTokenType::PROPERTY,
                    SemanticTokenType::STRING,
                    SemanticTokenType::NUMBER,
                    SemanticTokenType::ENUM_MEMBER,
                    SemanticTokenType::COMMENT,
                    SemanticTokenType::OPERATOR,
                ],
                token_modifiers: Vec::new(),
            },
            token_indices: [0, 1, 2, 3, 4, 5, 6],
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct TokenSpan {
    start: usize,
    end: usize,
    syntax_class: SyntaxClass,
}

pub(crate) fn semantic_tokens(
    text: &str,
    line_index: &LineIndex,
    encoding: PositionEncoding,
    requested_range: Option<Range>,
    legend: &SemanticLegend,
    cancelled: &AtomicBool,
) -> Option<Vec<SemanticToken>> {
    let (requested_start, requested_end, first_line, last_line) = match requested_range {
        Some(range) => {
            let start = line_index.byte_offset(text, range.start, encoding)?;
            let end = line_index.byte_offset(text, range.end, encoding)?;
            if start >= end {
                return Some(Vec::new());
            }
            (
                start,
                end,
                usize::try_from(range.start.line).ok()?,
                usize::try_from(range.end.line).ok()?,
            )
        }
        None => (0, text.len(), 0, line_index.line_count().saturating_sub(1)),
    };

    let mut tokens = Vec::new();
    let mut previous_line = 0_u32;
    let mut previous_start = 0_u32;
    let mut emitted = false;
    for line_number in first_line..=last_line.min(line_index.line_count().saturating_sub(1)) {
        if cancelled.load(Ordering::Relaxed) {
            return None;
        }
        let Some((line_start, line_end)) = line_index.line_bounds(text, line_number) else {
            continue;
        };
        let Some(line) = text.get(line_start..line_end) else {
            continue;
        };
        let line_u32 = u32::try_from(line_number).unwrap_or(u32::MAX);
        let spans = scan_line(line);
        let mut consumed_byte = 0_usize;
        let mut consumed_units = 0_usize;
        for span in spans {
            let absolute_start = line_start.saturating_add(span.start);
            let absolute_end = line_start.saturating_add(span.end);
            let clipped_start = absolute_start.max(requested_start);
            let clipped_end = absolute_end.min(requested_end);
            if clipped_start >= clipped_end {
                continue;
            }
            let relative_start = clipped_start.saturating_sub(line_start);
            let relative_end = clipped_end.saturating_sub(line_start);
            consumed_units = consumed_units.saturating_add(
                encoding.units(line.get(consumed_byte..relative_start).unwrap_or_default()),
            );
            let token_length =
                encoding.units(line.get(relative_start..relative_end).unwrap_or_default());
            consumed_byte = relative_end;
            let start = u32::try_from(consumed_units).unwrap_or(u32::MAX);
            let length = u32::try_from(token_length).unwrap_or(u32::MAX);
            if length == 0 {
                continue;
            }
            let delta_line = if emitted {
                line_u32.saturating_sub(previous_line)
            } else {
                line_u32
            };
            let delta_start = if emitted && delta_line == 0 {
                start.saturating_sub(previous_start)
            } else {
                start
            };
            tokens.push(SemanticToken {
                delta_line,
                delta_start,
                length,
                token_type: legend.token_index(span.syntax_class),
                token_modifiers_bitset: 0,
            });
            emitted = true;
            previous_line = line_u32;
            previous_start = start;
            consumed_units = consumed_units.saturating_add(token_length);
        }
    }
    Some(tokens)
}

fn scan_line(line: &str) -> Vec<TokenSpan> {
    let bytes = line.as_bytes();
    let first_content = bytes
        .iter()
        .position(|byte| !matches!(byte, b' ' | b'\t'))
        .unwrap_or(bytes.len());
    if bytes.get(first_content) == Some(&b'#') {
        return vec![TokenSpan {
            start: first_content,
            end: bytes.len(),
            syntax_class: SyntaxClass::Comment,
        }];
    }

    let mut spans = Vec::new();
    let mut index = first_content;
    while index < bytes.len() {
        match bytes[index] {
            b' ' | b'\t' => index += 1,
            b'"' => {
                let end = string_end(bytes, index);
                spans.push(TokenSpan {
                    start: index,
                    end,
                    syntax_class: SyntaxClass::String,
                });
                index = end;
            }
            b'-' if bytes.get(index + 1).is_some_and(u8::is_ascii_digit) => {
                let end = number_end(bytes, index + 1);
                spans.push(TokenSpan {
                    start: index,
                    end,
                    syntax_class: SyntaxClass::Number,
                });
                index = end;
            }
            byte if byte.is_ascii_digit() => {
                let end = number_end(bytes, index);
                spans.push(TokenSpan {
                    start: index,
                    end,
                    syntax_class: SyntaxClass::Number,
                });
                index = end;
            }
            byte if is_identifier_start(byte) => {
                let end = identifier_end(bytes, index);
                let value = line.get(index..end).unwrap_or_default();
                spans.push(TokenSpan {
                    start: index,
                    end,
                    syntax_class: classify_identifier(value),
                });
                index = end;
            }
            b'=' | b'{' | b'}' | b'[' | b']' | b',' => {
                spans.push(TokenSpan {
                    start: index,
                    end: index + 1,
                    syntax_class: SyntaxClass::Operator,
                });
                index += 1;
            }
            _ => index += 1,
        }
    }
    spans
}

fn string_end(bytes: &[u8], start: usize) -> usize {
    let mut index = start.saturating_add(1);
    let mut escaped = false;
    while index < bytes.len() {
        let byte = bytes[index];
        index += 1;
        if escaped {
            escaped = false;
        } else if byte == b'\\' {
            escaped = true;
        } else if byte == b'"' {
            break;
        }
    }
    index
}

fn number_end(bytes: &[u8], start: usize) -> usize {
    let mut end = start;
    while bytes.get(end).is_some_and(u8::is_ascii_digit) {
        end += 1;
    }
    end
}

fn identifier_end(bytes: &[u8], start: usize) -> usize {
    let mut end = start;
    while bytes
        .get(end)
        .is_some_and(|byte| is_identifier_continue(*byte))
    {
        end += 1;
    }
    end
}

const fn is_identifier_start(byte: u8) -> bool {
    byte.is_ascii_alphabetic() || matches!(byte, b'_' | b'@')
}

const fn is_identifier_continue(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'@')
}

fn classify_identifier(value: &str) -> SyntaxClass {
    if value == "@lokit" || BLOCK_NAMES.contains(&value) {
        SyntaxClass::Keyword
    } else if ENUM_VALUES.contains(&value) {
        SyntaxClass::EnumMember
    } else {
        SyntaxClass::Property
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use tower_lsp_server::ls_types::{Position, SemanticTokenType};

    use super::*;

    fn absolute_tokens(tokens: &[SemanticToken]) -> Vec<(u32, u32, u32, u32)> {
        let mut line = 0_u32;
        let mut start = 0_u32;
        tokens
            .iter()
            .map(|token| {
                line = line.saturating_add(token.delta_line);
                start = if token.delta_line == 0 {
                    start.saturating_add(token.delta_start)
                } else {
                    token.delta_start
                };
                (line, start, token.length, token.token_type)
            })
            .collect()
    }

    #[test]
    fn covers_every_lokit_syntax_class() {
        let text = concat!(
            "@lokit 1\n",
            "document {\n",
            "  source_locale = \"en\"\n",
            "}\n",
            "unit \"hello\" {\n",
            "  status = approved\n",
            "  # translator note\n",
            "}\n",
        );
        let legend = SemanticLegend::canonical();
        let cancelled = AtomicBool::new(false);
        let tokens = semantic_tokens(
            text,
            &LineIndex::new(text),
            PositionEncoding::Utf16,
            None,
            &legend,
            &cancelled,
        )
        .unwrap_or_default();
        let classes: HashSet<_> = tokens.iter().map(|token| token.token_type).collect();
        assert_eq!(classes, HashSet::from([0, 1, 2, 3, 4, 5, 6]));
    }

    #[test]
    fn positions_and_lengths_follow_every_negotiated_encoding() {
        let text = "é source = \"é😀\"\n  # café 😀\n";
        let index = LineIndex::new(text);
        let legend = SemanticLegend::canonical();
        let cancelled = AtomicBool::new(false);

        let utf8 = semantic_tokens(
            text,
            &index,
            PositionEncoding::Utf8,
            None,
            &legend,
            &cancelled,
        )
        .map(|tokens| absolute_tokens(&tokens))
        .unwrap_or_default();
        let utf16 = semantic_tokens(
            text,
            &index,
            PositionEncoding::Utf16,
            None,
            &legend,
            &cancelled,
        )
        .map(|tokens| absolute_tokens(&tokens))
        .unwrap_or_default();
        let utf32 = semantic_tokens(
            text,
            &index,
            PositionEncoding::Utf32,
            None,
            &legend,
            &cancelled,
        )
        .map(|tokens| absolute_tokens(&tokens))
        .unwrap_or_default();

        assert!(utf8.contains(&(0, 3, 6, 1)));
        assert!(utf8.contains(&(0, 12, 8, 2)));
        assert!(utf16.contains(&(0, 2, 6, 1)));
        assert!(utf16.contains(&(0, 11, 5, 2)));
        assert!(utf32.contains(&(0, 2, 6, 1)));
        assert!(utf32.contains(&(0, 11, 4, 2)));
        assert!(utf8.contains(&(1, 2, 12, 5)));
        assert!(utf16.contains(&(1, 2, 9, 5)));
        assert!(utf32.contains(&(1, 2, 8, 5)));
    }

    #[test]
    fn range_tokens_are_clipped_without_scanning_other_lines() {
        let text = "source = \"café\"\ntarget = \"bonjour\"\n";
        let index = LineIndex::new(text);
        let legend = SemanticLegend::canonical();
        let cancelled = AtomicBool::new(false);
        let tokens = semantic_tokens(
            text,
            &index,
            PositionEncoding::Utf16,
            Some(Range::new(Position::new(0, 10), Position::new(0, 13))),
            &legend,
            &cancelled,
        )
        .map(|tokens| absolute_tokens(&tokens))
        .unwrap_or_default();
        assert_eq!(tokens, vec![(0, 10, 3, 2)]);
    }

    #[test]
    fn negotiates_only_relative_client_token_types() {
        let unsupported = SemanticTokensClientCapabilities {
            requests: SemanticTokensClientCapabilitiesRequests::default(),
            token_types: vec![SemanticTokenType::STRING],
            token_modifiers: Vec::new(),
            formats: Vec::new(),
            ..SemanticTokensClientCapabilities::default()
        };
        assert!(SemanticLegend::negotiate(&unsupported).is_none());

        let supported = SemanticTokensClientCapabilities {
            formats: vec![TokenFormat::RELATIVE],
            ..unsupported
        };
        let negotiated = SemanticLegend::negotiate(&supported);
        assert!(negotiated.is_some());
        if let Some(negotiated) = negotiated {
            assert_eq!(
                negotiated.lsp().token_types,
                vec![SemanticTokenType::STRING]
            );
        }
    }
}
