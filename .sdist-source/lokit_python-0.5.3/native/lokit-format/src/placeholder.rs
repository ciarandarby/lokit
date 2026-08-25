//! Bounded placeholder detection, projection, canonicalization, and reformation.
//!
//! The scanners in this module never evaluate a placeholder expression.  They
//! only recognize the structural spelling needed by localization tooling.  A
//! scan retains the exact source bytes and records the smallest argument-name
//! span that can be safely rewritten in a translated message.

use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::fmt;
use std::ops::Range;
use std::str::FromStr;

use crate::{CodePart, Data, SegmentPart, TextPart, TieData, TieType};

pub const ATTRIBUTE_KIND: &str = "lokit.placeholder.kind";
pub const ATTRIBUTE_KEY: &str = "lokit.placeholder.key";
pub const ATTRIBUTE_ROLE: &str = "lokit.placeholder.role";
pub const ATTRIBUTE_SEQUENCE: &str = "lokit.placeholder.sequence";
pub const ATTRIBUTE_SYNTAX: &str = "lokit.placeholder.syntax";
pub const ATTRIBUTE_TOKEN: &str = "lokit.placeholder.token";
pub const ATTRIBUTE_VALUE_TYPE: &str = "lokit.placeholder.value_type";

const KIND_INLINE: &str = "inline";
const KIND_RUNTIME: &str = "runtime";

const CANONICAL_SEPARATOR: char = '\u{1f}';
const DEFAULT_MAX_INPUT_BYTES: usize = 64 * 1024 * 1024;
const DEFAULT_MAX_OCCURRENCES: usize = 4096;
const DEFAULT_MAX_PLACEHOLDER_BYTES: usize = 64 * 1024;
const DEFAULT_MAX_NESTING: usize = 64;

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum PlaceholderSyntax {
    CPrintf,
    ObjectiveCPrintf,
    CxxPrintf,
    CxxStdFormat,
    PythonPercent,
    PythonBrace,
    JavaMessageFormat,
    JavaFormatter,
    IcuMessageFormat1,
    UnicodeMessageFormat2,
    DotNetComposite,
    JavaScriptPrintf,
    EcmaScriptTemplate,
    RustFormat,
    GoFormat,
    RubyFormat,
    PhpPrintf,
    ShellPrintf,
    AwkPrintf,
    LuaPrintf,
    ObjectPascal,
    Modula2Printf,
    DFormat,
    OcamlPrintf,
    QtArg,
    QtPlural,
    Kde,
    KdeKuit,
    Boost,
    TclPrintf,
    PerlPrintf,
    PerlBrace,
    Scheme,
    Lisp,
    Elisp,
    Librep,
    Smalltalk,
    Fluent,
    Mustache,
    Handlebars,
    ShellParameter,
    SwiftInterpolation,
    GccInternal,
    GfcInternal,
    Ycp,
}

impl PlaceholderSyntax {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::CPrintf => "c-printf",
            Self::ObjectiveCPrintf => "objective-c-printf",
            Self::CxxPrintf => "cxx-printf",
            Self::CxxStdFormat => "cxx-std-format",
            Self::PythonPercent => "python-percent",
            Self::PythonBrace => "python-brace",
            Self::JavaMessageFormat => "java-message-format",
            Self::JavaFormatter => "java-formatter",
            Self::IcuMessageFormat1 => "icu-message-format-1",
            Self::UnicodeMessageFormat2 => "unicode-message-format-2",
            Self::DotNetComposite => "dotnet-composite",
            Self::JavaScriptPrintf => "javascript-printf",
            Self::EcmaScriptTemplate => "ecmascript-template",
            Self::RustFormat => "rust-format",
            Self::GoFormat => "go-format",
            Self::RubyFormat => "ruby-format",
            Self::PhpPrintf => "php-printf",
            Self::ShellPrintf => "shell-printf",
            Self::AwkPrintf => "awk-printf",
            Self::LuaPrintf => "lua-printf",
            Self::ObjectPascal => "object-pascal",
            Self::Modula2Printf => "modula2-printf",
            Self::DFormat => "d-format",
            Self::OcamlPrintf => "ocaml-printf",
            Self::QtArg => "qt-arg",
            Self::QtPlural => "qt-plural",
            Self::Kde => "kde",
            Self::KdeKuit => "kde-kuit",
            Self::Boost => "boost",
            Self::TclPrintf => "tcl-printf",
            Self::PerlPrintf => "perl-printf",
            Self::PerlBrace => "perl-brace",
            Self::Scheme => "scheme",
            Self::Lisp => "lisp",
            Self::Elisp => "elisp",
            Self::Librep => "librep",
            Self::Smalltalk => "smalltalk",
            Self::Fluent => "fluent",
            Self::Mustache => "mustache",
            Self::Handlebars => "handlebars",
            Self::ShellParameter => "shell-parameter",
            Self::SwiftInterpolation => "swift-interpolation",
            Self::GccInternal => "gcc-internal",
            Self::GfcInternal => "gfc-internal",
            Self::Ycp => "ycp",
        }
    }

    const fn family(self) -> PlaceholderFamily {
        match self {
            Self::CPrintf
            | Self::ObjectiveCPrintf
            | Self::CxxPrintf
            | Self::PythonPercent
            | Self::JavaFormatter
            | Self::JavaScriptPrintf
            | Self::GoFormat
            | Self::RubyFormat
            | Self::PhpPrintf
            | Self::ShellPrintf
            | Self::AwkPrintf
            | Self::LuaPrintf
            | Self::Modula2Printf
            | Self::DFormat
            | Self::OcamlPrintf
            | Self::TclPrintf
            | Self::PerlPrintf
            | Self::Elisp => PlaceholderFamily::Printf,
            Self::CxxStdFormat
            | Self::PythonBrace
            | Self::RustFormat
            | Self::DotNetComposite
            | Self::PerlBrace => PlaceholderFamily::Brace,
            Self::JavaMessageFormat | Self::IcuMessageFormat1 => PlaceholderFamily::Message,
            Self::UnicodeMessageFormat2 => PlaceholderFamily::Message2,
            Self::ObjectPascal => PlaceholderFamily::ObjectPascal,
            Self::QtArg
            | Self::QtPlural
            | Self::Kde
            | Self::KdeKuit
            | Self::Smalltalk
            | Self::Ycp => PlaceholderFamily::Qt,
            Self::Boost => PlaceholderFamily::Boost,
            Self::Scheme | Self::Lisp | Self::Librep => PlaceholderFamily::Tilde,
            Self::Fluent => PlaceholderFamily::Fluent,
            Self::Mustache | Self::Handlebars => PlaceholderFamily::Mustache,
            Self::EcmaScriptTemplate => PlaceholderFamily::EcmaScript,
            Self::ShellParameter => PlaceholderFamily::Shell,
            Self::SwiftInterpolation => PlaceholderFamily::Swift,
            Self::GccInternal | Self::GfcInternal => PlaceholderFamily::Compiler,
        }
    }
}

impl fmt::Display for PlaceholderSyntax {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for PlaceholderSyntax {
    type Err = PlaceholderError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let normalized = value.trim().to_ascii_lowercase().replace('_', "-");
        let syntax = match normalized.as_str() {
            "c" | "c-format" | "c-printf" | "printf" => Self::CPrintf,
            "objc" | "objc-format" | "objective-c" | "objective-c-printf" => Self::ObjectiveCPrintf,
            "cxx-printf" => Self::CxxPrintf,
            "c++-format" | "cxx-format" | "c++-brace" | "cxx-std-format" | "std-format" => {
                Self::CxxStdFormat
            }
            "python-format" | "python-percent" => Self::PythonPercent,
            "python-brace-format" | "python-brace" => Self::PythonBrace,
            "java-format" | "java-message-format" => Self::JavaMessageFormat,
            "java-printf-format" | "java-formatter" => Self::JavaFormatter,
            "icu" | "icu-message-format" | "icu-message-format-1" => Self::IcuMessageFormat1,
            "mf2" | "message-format-2" | "unicode-message-format-2" => Self::UnicodeMessageFormat2,
            "csharp-format" | "dotnet" | "dotnet-composite" => Self::DotNetComposite,
            "javascript-format" | "javascript-printf" => Self::JavaScriptPrintf,
            "ecmascript-template" | "javascript-template" => Self::EcmaScriptTemplate,
            "rust-format" => Self::RustFormat,
            "go-format" => Self::GoFormat,
            "ruby-format" => Self::RubyFormat,
            "php-format" | "php-printf" => Self::PhpPrintf,
            "sh-printf-format" | "shell-printf" => Self::ShellPrintf,
            "awk-format" | "awk-printf" => Self::AwkPrintf,
            "lua-format" | "lua-printf" => Self::LuaPrintf,
            "object-pascal-format" | "object-pascal" => Self::ObjectPascal,
            "modula2-format" | "modula-2-format" | "modula2-printf" => Self::Modula2Printf,
            "d-format" => Self::DFormat,
            "ocaml-format" | "ocaml-printf" => Self::OcamlPrintf,
            "qt-format" | "qt-arg" => Self::QtArg,
            "qt-plural-format" | "qt-plural" => Self::QtPlural,
            "kde-format" | "kde" => Self::Kde,
            "kde-kuit-format" | "kde-kuit" | "kuit" => Self::KdeKuit,
            "boost-format" | "boost" => Self::Boost,
            "tcl-format" | "tcl-printf" => Self::TclPrintf,
            "perl-format" | "perl-printf" => Self::PerlPrintf,
            "perl-brace-format" | "perl-brace" => Self::PerlBrace,
            "scheme-format" | "scheme" => Self::Scheme,
            "lisp-format" | "lisp" => Self::Lisp,
            "elisp-format" | "elisp" => Self::Elisp,
            "librep-format" | "librep" => Self::Librep,
            "smalltalk-format" | "smalltalk" => Self::Smalltalk,
            "fluent" => Self::Fluent,
            "mustache" => Self::Mustache,
            "handlebars" => Self::Handlebars,
            "sh-format" | "shell" | "shell-parameter" => Self::ShellParameter,
            "swift" | "swift-interpolation" => Self::SwiftInterpolation,
            "gcc-internal-format" | "gcc-internal" => Self::GccInternal,
            "gfc-internal-format" | "gfc-internal" => Self::GfcInternal,
            "ycp-format" | "ycp" => Self::Ycp,
            _ => return Err(PlaceholderError::UnknownSyntax(value.to_owned())),
        };
        Ok(syntax)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PlaceholderRole {
    Value,
    Width,
    Precision,
    Selector,
    Markup,
}

impl PlaceholderRole {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Value => "value",
            Self::Width => "width",
            Self::Precision => "precision",
            Self::Selector => "selector",
            Self::Markup => "markup",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PlaceholderValueType {
    Any,
    String,
    Integer,
    Number,
    Float,
    Character,
    Date,
    Time,
    Pointer,
    Count,
}

impl PlaceholderValueType {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Any => "any",
            Self::String => "string",
            Self::Integer => "integer",
            Self::Number => "number",
            Self::Float => "float",
            Self::Character => "character",
            Self::Date => "date",
            Self::Time => "time",
            Self::Pointer => "pointer",
            Self::Count => "count",
        }
    }

    fn compatible_with(self, other: Self) -> bool {
        self == other
            || matches!(self, Self::Any)
            || matches!(other, Self::Any)
            || matches!(
                (self, other),
                (
                    Self::Integer | Self::Float | Self::Number,
                    Self::Integer | Self::Float | Self::Number
                )
            )
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum PlaceholderFamily {
    Printf,
    Brace,
    Message,
    Message2,
    Qt,
    ObjectPascal,
    Boost,
    Tilde,
    Fluent,
    Mustache,
    EcmaScript,
    Shell,
    Swift,
    Compiler,
}

impl PlaceholderFamily {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Printf => "printf",
            Self::Brace => "brace",
            Self::Message => "message",
            Self::Message2 => "message2",
            Self::Qt => "qt",
            Self::ObjectPascal => "object-pascal",
            Self::Boost => "boost",
            Self::Tilde => "tilde",
            Self::Fluent => "fluent",
            Self::Mustache => "mustache",
            Self::EcmaScript => "ecmascript",
            Self::Shell => "shell",
            Self::Swift => "swift",
            Self::Compiler => "compiler",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PlaceholderLimits {
    pub max_input_bytes: usize,
    pub max_occurrences: usize,
    pub max_placeholder_bytes: usize,
    pub max_nesting: usize,
}

impl Default for PlaceholderLimits {
    fn default() -> Self {
        Self {
            max_input_bytes: DEFAULT_MAX_INPUT_BYTES,
            max_occurrences: DEFAULT_MAX_OCCURRENCES,
            max_placeholder_bytes: DEFAULT_MAX_PLACEHOLDER_BYTES,
            max_nesting: DEFAULT_MAX_NESTING,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DetectionOptions {
    pub syntaxes: Vec<PlaceholderSyntax>,
    pub disabled_syntaxes: Vec<PlaceholderSyntax>,
    pub auto_detect: bool,
    pub limits: PlaceholderLimits,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlaceholderProjectionOptions {
    pub detection: DetectionOptions,
    pub runtime_placeholders: bool,
    pub inline_placeholders: bool,
    pub project_targets: bool,
}

impl Default for PlaceholderProjectionOptions {
    fn default() -> Self {
        Self {
            detection: DetectionOptions::default(),
            runtime_placeholders: true,
            inline_placeholders: true,
            project_targets: true,
        }
    }
}

impl Default for DetectionOptions {
    fn default() -> Self {
        Self {
            syntaxes: Vec::new(),
            disabled_syntaxes: Vec::new(),
            auto_detect: true,
            limits: PlaceholderLimits::default(),
        }
    }
}

impl DetectionOptions {
    pub fn explicit(syntaxes: impl IntoIterator<Item = PlaceholderSyntax>) -> Self {
        Self {
            syntaxes: unique_syntaxes(syntaxes),
            auto_detect: false,
            ..Self::default()
        }
    }

    pub fn from_hints(
        explicit: impl IntoIterator<Item = PlaceholderSyntax>,
        gettext_flags: &[String],
        auto_detect: bool,
    ) -> Self {
        let mut syntaxes = unique_syntaxes(explicit);
        let mut disabled_syntaxes = Vec::new();
        for raw_flags in gettext_flags {
            for raw_flag in raw_flags.split([',', ' ', '\t', '\r', '\n']) {
                let flag = raw_flag.trim().to_ascii_lowercase();
                if flag.is_empty() {
                    continue;
                }
                if let Some(name) = flag.strip_prefix("no-") {
                    if let Some(syntax) = gettext_syntax(name) {
                        push_unique(&mut disabled_syntaxes, syntax);
                        syntaxes.retain(|candidate| *candidate != syntax);
                    }
                    continue;
                }
                let name = flag
                    .strip_prefix("possible-")
                    .or_else(|| flag.strip_prefix("pass-"))
                    .unwrap_or(&flag);
                if let Some(syntax) = gettext_syntax(name) {
                    push_unique(&mut syntaxes, syntax);
                }
            }
        }
        let has_positive_hint = !syntaxes.is_empty();
        Self {
            syntaxes,
            disabled_syntaxes,
            auto_detect: auto_detect && !has_positive_hint,
            limits: PlaceholderLimits::default(),
        }
    }

    fn is_disabled(&self, syntax: PlaceholderSyntax) -> bool {
        self.disabled_syntaxes.contains(&syntax)
    }

    fn syntax_for_family(&self, family: PlaceholderFamily) -> Option<PlaceholderSyntax> {
        self.syntaxes
            .iter()
            .copied()
            .find(|syntax| syntax.family() == family && !self.is_disabled(*syntax))
            .or_else(|| self.auto_syntax(family))
    }

    fn explicit_syntaxes_for_family(
        &self,
        family: PlaceholderFamily,
    ) -> impl Iterator<Item = PlaceholderSyntax> + '_ {
        self.syntaxes
            .iter()
            .copied()
            .filter(move |syntax| syntax.family() == family && !self.is_disabled(*syntax))
    }

    fn auto_syntax(&self, family: PlaceholderFamily) -> Option<PlaceholderSyntax> {
        if !self.auto_detect {
            return None;
        }
        let syntax = match family {
            PlaceholderFamily::Printf => PlaceholderSyntax::CPrintf,
            PlaceholderFamily::Brace => PlaceholderSyntax::PythonBrace,
            PlaceholderFamily::Qt => PlaceholderSyntax::QtArg,
            PlaceholderFamily::Fluent => PlaceholderSyntax::Fluent,
            PlaceholderFamily::Mustache => PlaceholderSyntax::Mustache,
            PlaceholderFamily::EcmaScript => PlaceholderSyntax::EcmaScriptTemplate,
            PlaceholderFamily::Boost
            | PlaceholderFamily::Message
            | PlaceholderFamily::Message2
            | PlaceholderFamily::Tilde
            | PlaceholderFamily::Shell
            | PlaceholderFamily::Swift
            | PlaceholderFamily::ObjectPascal
            | PlaceholderFamily::Compiler => return None,
        };
        (!self.is_disabled(syntax)).then_some(syntax)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlaceholderOccurrence {
    pub syntax: PlaceholderSyntax,
    pub role: PlaceholderRole,
    pub value_type: PlaceholderValueType,
    pub key: String,
    pub range: Range<usize>,
    pub key_range: Option<Range<usize>>,
    pub original_text: String,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct PlaceholderAnalysis {
    pub occurrences: Vec<PlaceholderOccurrence>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PlaceholderProjection {
    pub text: String,
    pub tag_map: Vec<(String, TieData)>,
    pub parts: Vec<SegmentPart>,
    pub token_prefix: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ResolvedPlaceholders {
    pub text: String,
    pub tag_map: Vec<(String, TieData)>,
    pub parts: Vec<SegmentPart>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CanonicalPlaceholderText {
    pub text: String,
    pub signature: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReformedTarget {
    pub text: String,
    pub changed: bool,
    pub signature: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PlaceholderError {
    InputLimitExceeded {
        actual: usize,
        limit: usize,
    },
    OccurrenceLimitExceeded {
        limit: usize,
    },
    PlaceholderLimitExceeded {
        start: usize,
        limit: usize,
    },
    NestingLimitExceeded {
        start: usize,
        limit: usize,
    },
    TokenNamespaceExhausted,
    DuplicateTagMapKey {
        key: String,
    },
    DuplicateCodeReference {
        reference: String,
    },
    DanglingCodeReference {
        reference: String,
    },
    UnreferencedTag {
        key: String,
    },
    PartsTextMismatch,
    ProjectionMetadataConflict {
        key: String,
    },
    InvalidProjectionToken {
        token: String,
    },
    ProjectionSequenceMismatch {
        expected: usize,
        actual: usize,
    },
    MissingOriginalPlaceholderText {
        key: String,
    },
    UnknownSyntax(String),
    IncompatibleSourceSignatures {
        candidate: String,
        query: String,
    },
    UnmappedTargetPlaceholder {
        key: String,
        syntax: PlaceholderSyntax,
    },
    IncompatibleTargetPlaceholder {
        key: String,
        syntax: PlaceholderSyntax,
    },
}

impl fmt::Display for PlaceholderError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InputLimitExceeded { actual, limit } => {
                write!(formatter, "placeholder input is {actual} bytes; limit is {limit}")
            }
            Self::OccurrenceLimitExceeded { limit } => {
                write!(formatter, "placeholder occurrence limit {limit} exceeded")
            }
            Self::PlaceholderLimitExceeded { start, limit } => write!(
                formatter,
                "placeholder at byte {start} exceeds the {limit}-byte limit"
            ),
            Self::NestingLimitExceeded { start, limit } => write!(
                formatter,
                "placeholder at byte {start} exceeds the nesting limit {limit}"
            ),
            Self::TokenNamespaceExhausted => {
                formatter.write_str("could not allocate a collision-free placeholder token namespace")
            }
            Self::DuplicateTagMapKey { key } => {
                write!(formatter, "duplicate tag-map key {key:?}")
            }
            Self::DuplicateCodeReference { reference } => {
                write!(formatter, "duplicate code-part reference {reference:?}")
            }
            Self::DanglingCodeReference { reference } => {
                write!(formatter, "code-part reference {reference:?} has no tag-map entry")
            }
            Self::UnreferencedTag { key } => {
                write!(formatter, "tag-map entry {key:?} has no code-part reference")
            }
            Self::PartsTextMismatch => {
                formatter.write_str("text parts do not reproduce the associated plain text")
            }
            Self::ProjectionMetadataConflict { key } => write!(
                formatter,
                "tag-map entry {key:?} already contains Lokit placeholder projection metadata"
            ),
            Self::InvalidProjectionToken { token } => {
                write!(formatter, "invalid projected placeholder token {token:?}")
            }
            Self::ProjectionSequenceMismatch { expected, actual } => write!(
                formatter,
                "projected placeholder sequence expected {expected}, got {actual}"
            ),
            Self::MissingOriginalPlaceholderText { key } => write!(
                formatter,
                "runtime placeholder {key:?} has no exact original text"
            ),
            Self::UnknownSyntax(value) => write!(formatter, "unknown placeholder syntax {value:?}"),
            Self::IncompatibleSourceSignatures { candidate, query } => write!(
                formatter,
                "candidate placeholder signature {candidate:?} is incompatible with query signature {query:?}"
            ),
            Self::UnmappedTargetPlaceholder { key, syntax } => write!(
                formatter,
                "target placeholder {key:?} ({syntax}) does not map to the candidate source"
            ),
            Self::IncompatibleTargetPlaceholder { key, syntax } => write!(
                formatter,
                "target placeholder {key:?} ({syntax}) has an incompatible role or value type"
            ),
        }
    }
}

impl Error for PlaceholderError {}

#[derive(Clone, Debug)]
struct ParsedOccurrence {
    end: usize,
    key: String,
    key_range: Option<Range<usize>>,
    role: PlaceholderRole,
    value_type: PlaceholderValueType,
    syntax: PlaceholderSyntax,
}

pub fn detect_placeholders(
    text: &str,
    options: &DetectionOptions,
) -> Result<PlaceholderAnalysis, PlaceholderError> {
    if text.len() > options.limits.max_input_bytes {
        return Err(PlaceholderError::InputLimitExceeded {
            actual: text.len(),
            limit: options.limits.max_input_bytes,
        });
    }

    let bytes = text.as_bytes();
    let mut occurrences = Vec::new();
    let mut cursor = 0;
    let mut implicit_counts = Vec::<(PlaceholderFamily, usize)>::new();
    while cursor < bytes.len() {
        // A doubled opening brace is an escape in brace-format families.  Do
        // not advance one byte and accidentally recognize its second half as
        // a standalone field when a Mustache-family profile is not active.
        if bytes[cursor] == b'{'
            && bytes.get(cursor + 1) == Some(&b'{')
            && options
                .syntax_for_family(PlaceholderFamily::Mustache)
                .is_none()
        {
            cursor += 2;
            continue;
        }
        // `${...}` belongs to a dollar-expression grammar.  If that grammar
        // rejects it (for example a shell operator during conservative auto
        // detection), its inner `{...}` must not be reclassified as a brace
        // placeholder on the following byte.
        if bytes[cursor] == b'{' && cursor > 0 && bytes[cursor - 1] == b'$' {
            cursor += 1;
            continue;
        }
        let parsed = match bytes[cursor] {
            b'{' => parse_braced_at(text, cursor, options)?,
            b'$' => parse_dollar_at(text, cursor, options)?,
            b'%' => parse_percent_at(text, cursor, options)?,
            b'~' => parse_tilde_at(text, cursor, options)?,
            b'\\' if bytes.get(cursor + 1) == Some(&b'(') => parse_swift_at(text, cursor, options)?,
            _ => None,
        };

        let Some(mut parsed) = parsed else {
            cursor += text[cursor..].chars().next().map_or(1, char::len_utf8);
            continue;
        };
        if parsed.end <= cursor || parsed.end > text.len() {
            cursor += text[cursor..].chars().next().map_or(1, char::len_utf8);
            continue;
        }
        if parsed.end - cursor > options.limits.max_placeholder_bytes {
            return Err(PlaceholderError::PlaceholderLimitExceeded {
                start: cursor,
                limit: options.limits.max_placeholder_bytes,
            });
        }
        if parsed.key.is_empty() {
            let family = parsed.syntax.family();
            let family_index = if let Some(index) = implicit_counts
                .iter()
                .position(|(candidate, _)| *candidate == family)
            {
                index
            } else {
                implicit_counts.push((family, 0));
                implicit_counts.len() - 1
            };
            let count = &mut implicit_counts[family_index].1;
            parsed.key = format!("@{count}");
            *count += 1;
        }
        occurrences.push(PlaceholderOccurrence {
            syntax: parsed.syntax,
            role: parsed.role,
            value_type: parsed.value_type,
            key: parsed.key,
            range: cursor..parsed.end,
            key_range: parsed.key_range,
            original_text: text[cursor..parsed.end].to_owned(),
        });
        if occurrences.len() > options.limits.max_occurrences {
            return Err(PlaceholderError::OccurrenceLimitExceeded {
                limit: options.limits.max_occurrences,
            });
        }
        cursor = parsed.end;
    }
    Ok(PlaceholderAnalysis { occurrences })
}

pub fn project_placeholders(
    text: &str,
    options: &DetectionOptions,
) -> Result<PlaceholderProjection, PlaceholderError> {
    let projection_options = PlaceholderProjectionOptions {
        detection: options.clone(),
        runtime_placeholders: true,
        inline_placeholders: false,
        project_targets: false,
    };
    project_segment_placeholders(text, &[], &[], &projection_options)
}

/// Project runtime placeholders and existing inline codes into one ordered,
/// collision-safe marker stream.
///
/// `text` is the plain text reproduced by the `Text` parts; `Code` parts do not
/// contribute bytes before projection.  The input graph is validated before any
/// output is constructed.  Existing inline `TieData` is retained and receives
/// only namespaced projection attributes, while detected runtime placeholders
/// become `placeholder.standalone` ties with their exact original spelling.
pub fn project_segment_placeholders(
    text: &str,
    parts: &[SegmentPart],
    tag_map: &[(String, TieData)],
    options: &PlaceholderProjectionOptions,
) -> Result<PlaceholderProjection, PlaceholderError> {
    let resolved = match resolve_projected_segment_placeholders(text, parts, tag_map) {
        Ok(resolved) => resolved,
        Err(error)
            if !has_projection_attributes(tag_map)
                && preserves_incomplete_native_structure(&error) =>
        {
            return Ok(PlaceholderProjection {
                text: text.to_owned(),
                tag_map: tag_map.to_vec(),
                parts: parts.to_vec(),
                token_prefix: collision_free_prefix(text),
            });
        }
        Err(error) => return Err(error),
    };
    if resolved.text.len() > options.detection.limits.max_input_bytes {
        return Err(PlaceholderError::InputLimitExceeded {
            actual: resolved.text.len(),
            limit: options.detection.limits.max_input_bytes,
        });
    }

    let token_prefix = collision_free_prefix(&resolved.text);
    let mut builder = SegmentProjectionBuilder::new(
        &resolved.text,
        &resolved.tag_map,
        options,
        token_prefix.clone(),
    )?;
    if resolved.parts.is_empty() {
        builder.project_text(&resolved.text)?;
    } else {
        for part in &resolved.parts {
            match part {
                SegmentPart::Text(part) => builder.project_text(&part.value)?,
                SegmentPart::Code(part) => builder.project_code(part)?,
            }
        }
    }
    Ok(PlaceholderProjection {
        text: builder.rendered,
        tag_map: builder.tag_map,
        parts: builder.parts,
        token_prefix,
    })
}

/// Validate and remove generic projection markers, restoring runtime
/// placeholders byte-for-byte and leaving native inline codes in their
/// original `CodePart` positions.
pub fn resolve_segment_placeholders(
    text: &str,
    parts: &[SegmentPart],
    tag_map: &[(String, TieData)],
) -> Result<ResolvedPlaceholders, PlaceholderError> {
    if !has_projection_attributes(tag_map) {
        return Ok(ResolvedPlaceholders {
            text: text.to_owned(),
            tag_map: tag_map.to_vec(),
            parts: parts.to_vec(),
        });
    }
    resolve_projected_segment_placeholders(text, parts, tag_map)
}

fn resolve_projected_segment_placeholders(
    text: &str,
    parts: &[SegmentPart],
    tag_map: &[(String, TieData)],
) -> Result<ResolvedPlaceholders, PlaceholderError> {
    let mut map = HashMap::with_capacity(tag_map.len());
    for (index, (key, _)) in tag_map.iter().enumerate() {
        if map.insert(key.as_str(), index).is_some() {
            return Err(PlaceholderError::DuplicateTagMapKey { key: key.clone() });
        }
    }
    if parts.is_empty() {
        if let Some((key, _)) = tag_map.first() {
            return Err(PlaceholderError::UnreferencedTag { key: key.clone() });
        }
        return Ok(ResolvedPlaceholders {
            text: text.to_owned(),
            tag_map: Vec::new(),
            parts: Vec::new(),
        });
    }

    let mut references = HashSet::with_capacity(tag_map.len());
    let mut runtime_keys = HashSet::new();
    let mut represented_text = String::with_capacity(text.len());
    let mut resolved_text = String::with_capacity(text.len());
    let mut resolved_parts = Vec::with_capacity(parts.len());
    let mut expected_sequence = 1_usize;
    let mut token_prefix: Option<String> = None;

    for part in parts {
        match part {
            SegmentPart::Text(part) => {
                represented_text.push_str(&part.value);
                push_resolved_text(&mut resolved_text, &mut resolved_parts, &part.value);
            }
            SegmentPart::Code(part) => {
                if !references.insert(part.r#ref.as_str()) {
                    return Err(PlaceholderError::DuplicateCodeReference {
                        reference: part.r#ref.clone(),
                    });
                }
                let index = *map.get(part.r#ref.as_str()).ok_or_else(|| {
                    PlaceholderError::DanglingCodeReference {
                        reference: part.r#ref.clone(),
                    }
                })?;
                let (key, tag) = &tag_map[index];
                let metadata = projection_metadata(key, tag)?;
                let Some(metadata) = metadata else {
                    resolved_parts.push(SegmentPart::Code(part.clone()));
                    continue;
                };

                represented_text.push_str(metadata.token);
                let (prefix, sequence) = parse_projection_token(metadata.token)?;
                if sequence != metadata.sequence || sequence != expected_sequence {
                    return Err(PlaceholderError::ProjectionSequenceMismatch {
                        expected: expected_sequence,
                        actual: sequence,
                    });
                }
                if let Some(existing) = token_prefix.as_deref() {
                    if existing != prefix {
                        return Err(PlaceholderError::InvalidProjectionToken {
                            token: metadata.token.to_owned(),
                        });
                    }
                } else {
                    token_prefix = Some(prefix.to_owned());
                }
                expected_sequence += 1;

                match metadata.kind {
                    ProjectionKind::Runtime => {
                        if tag.r#type != TieType::PlaceholderStandalone {
                            return Err(PlaceholderError::ProjectionMetadataConflict {
                                key: key.clone(),
                            });
                        }
                        let original = tag.original_text.as_deref().ok_or_else(|| {
                            PlaceholderError::MissingOriginalPlaceholderText { key: key.clone() }
                        })?;
                        push_resolved_text(&mut resolved_text, &mut resolved_parts, original);
                        runtime_keys.insert(key.as_str());
                    }
                    ProjectionKind::Inline => {
                        resolved_parts.push(SegmentPart::Code(part.clone()));
                    }
                }
            }
        }
    }
    if represented_text != text {
        return Err(PlaceholderError::PartsTextMismatch);
    }
    if let Some((key, _)) = tag_map
        .iter()
        .find(|(key, _)| !references.contains(key.as_str()))
    {
        return Err(PlaceholderError::UnreferencedTag { key: key.clone() });
    }

    let mut resolved_map = Vec::with_capacity(tag_map.len().saturating_sub(runtime_keys.len()));
    for (key, tag) in tag_map {
        if runtime_keys.contains(key.as_str()) {
            continue;
        }
        let mut tag = tag.clone();
        tag.attributes
            .retain(|(name, _)| !is_projection_attribute(name));
        resolved_map.push((key.clone(), tag));
    }
    Ok(ResolvedPlaceholders {
        text: resolved_text,
        tag_map: resolved_map,
        parts: resolved_parts,
    })
}

/// Validate a projected segment while retaining its generic marker text.
///
/// Projection-only codes become ordinary text parts, while native codes that
/// were not projected remain structured. This lets exporters deliberately
/// emit `{LOKIT_Pn}` markers as literal text without accidentally serializing
/// their backing `TieData` as native inline codes.
pub fn literalize_segment_placeholders(
    text: &str,
    parts: &[SegmentPart],
    tag_map: &[(String, TieData)],
) -> Result<ResolvedPlaceholders, PlaceholderError> {
    if !has_projection_attributes(tag_map) {
        return Ok(ResolvedPlaceholders {
            text: text.to_owned(),
            tag_map: tag_map.to_vec(),
            parts: parts.to_vec(),
        });
    }
    literalize_projected_segment_placeholders(text, parts, tag_map)
}

fn literalize_projected_segment_placeholders(
    text: &str,
    parts: &[SegmentPart],
    tag_map: &[(String, TieData)],
) -> Result<ResolvedPlaceholders, PlaceholderError> {
    let mut map = HashMap::with_capacity(tag_map.len());
    for (index, (key, _)) in tag_map.iter().enumerate() {
        if map.insert(key.as_str(), index).is_some() {
            return Err(PlaceholderError::DuplicateTagMapKey { key: key.clone() });
        }
    }
    if parts.is_empty() {
        if let Some((key, _)) = tag_map.first() {
            return Err(PlaceholderError::UnreferencedTag { key: key.clone() });
        }
        return Ok(ResolvedPlaceholders {
            text: text.to_owned(),
            tag_map: Vec::new(),
            parts: Vec::new(),
        });
    }

    let mut references = HashSet::with_capacity(tag_map.len());
    let mut projection_keys = HashSet::new();
    let mut represented_text = String::with_capacity(text.len());
    let mut literal_parts = Vec::with_capacity(parts.len());
    let mut expected_sequence = 1_usize;
    let mut token_prefix: Option<String> = None;

    for part in parts {
        match part {
            SegmentPart::Text(part) => {
                represented_text.push_str(&part.value);
                push_text_part(&mut literal_parts, &part.value);
            }
            SegmentPart::Code(part) => {
                if !references.insert(part.r#ref.as_str()) {
                    return Err(PlaceholderError::DuplicateCodeReference {
                        reference: part.r#ref.clone(),
                    });
                }
                let index = *map.get(part.r#ref.as_str()).ok_or_else(|| {
                    PlaceholderError::DanglingCodeReference {
                        reference: part.r#ref.clone(),
                    }
                })?;
                let (key, tag) = &tag_map[index];
                let Some(metadata) = projection_metadata(key, tag)? else {
                    literal_parts.push(SegmentPart::Code(part.clone()));
                    continue;
                };

                represented_text.push_str(metadata.token);
                let (prefix, sequence) = parse_projection_token(metadata.token)?;
                if sequence != metadata.sequence || sequence != expected_sequence {
                    return Err(PlaceholderError::ProjectionSequenceMismatch {
                        expected: expected_sequence,
                        actual: sequence,
                    });
                }
                if let Some(existing) = token_prefix.as_deref() {
                    if existing != prefix {
                        return Err(PlaceholderError::InvalidProjectionToken {
                            token: metadata.token.to_owned(),
                        });
                    }
                } else {
                    token_prefix = Some(prefix.to_owned());
                }
                expected_sequence += 1;
                projection_keys.insert(key.as_str());
                push_text_part(&mut literal_parts, metadata.token);
            }
        }
    }
    if represented_text != text {
        return Err(PlaceholderError::PartsTextMismatch);
    }
    if let Some((key, _)) = tag_map
        .iter()
        .find(|(key, _)| !references.contains(key.as_str()))
    {
        return Err(PlaceholderError::UnreferencedTag { key: key.clone() });
    }

    let literal_map = tag_map
        .iter()
        .filter(|(key, _)| !projection_keys.contains(key.as_str()))
        .cloned()
        .collect();
    Ok(ResolvedPlaceholders {
        text: text.to_owned(),
        tag_map: literal_map,
        parts: literal_parts,
    })
}

/// Apply placeholder projection to a complete Lokit unit without crossing the
/// Rust/Python boundary for any text, parts, or tag-map manipulation.
pub fn project_data_placeholders(
    mut data: Data,
    options: &PlaceholderProjectionOptions,
) -> Result<Data, PlaceholderError> {
    let had_tags = data.tags.is_some();
    let mut tags = data.tags.take().unwrap_or_default();

    let source_had_structure = !tags.source_tag_map.is_empty() || !tags.source_parts.is_empty();
    let source = project_segment_placeholders(
        &data.source,
        &tags.source_parts,
        &tags.source_tag_map,
        options,
    )?;
    if source_had_structure || !source.tag_map.is_empty() {
        data.source = source.text;
        tags.source_tag_map = source.tag_map;
        tags.source_parts = source.parts;
    }

    if options.project_targets {
        if let Some(target_text) = data.target.as_deref() {
            let target_had_structure =
                !tags.target_tag_map.is_empty() || !tags.target_parts.is_empty();
            let target = project_segment_placeholders(
                target_text,
                &tags.target_parts,
                &tags.target_tag_map,
                options,
            )?;
            if target_had_structure || !target.tag_map.is_empty() {
                data.target = Some(target.text);
                tags.target_tag_map = target.tag_map;
                tags.target_parts = target.parts;
            }
        }

        for (_, target) in &mut data.targets {
            let Some(target_text) = target.text.as_deref() else {
                if target.tags.as_ref().is_some_and(|target_tags| {
                    !target_tags.tag_map.is_empty() || !target_tags.parts.is_empty()
                }) {
                    return Err(PlaceholderError::PartsTextMismatch);
                }
                continue;
            };
            let had_target_tags = target.tags.is_some();
            let mut target_tags = target.tags.take().unwrap_or_default();
            let target_had_structure =
                !target_tags.tag_map.is_empty() || !target_tags.parts.is_empty();
            let projected = project_segment_placeholders(
                target_text,
                &target_tags.parts,
                &target_tags.tag_map,
                options,
            )?;
            if target_had_structure || !projected.tag_map.is_empty() {
                target.text = Some(projected.text);
                target_tags.tag_map = projected.tag_map;
                target_tags.parts = projected.parts;
                target.tags = Some(target_tags);
            } else if had_target_tags {
                target.tags = Some(target_tags);
            }
        }
    }

    if had_tags
        || !tags.source_tag_map.is_empty()
        || !tags.target_tag_map.is_empty()
        || !tags.source_parts.is_empty()
        || !tags.target_parts.is_empty()
    {
        data.tags = Some(tags);
    }
    Ok(data)
}

/// Resolve every projected segment in a complete Lokit unit before export.
pub fn resolve_data_placeholders(mut data: Data) -> Result<Data, PlaceholderError> {
    if let Some(mut tags) = data.tags.take() {
        let source =
            resolve_segment_placeholders(&data.source, &tags.source_parts, &tags.source_tag_map)?;
        data.source = source.text;
        tags.source_tag_map = source.tag_map;
        tags.source_parts = source.parts;

        if let Some(target_text) = data.target.as_deref() {
            let target = resolve_segment_placeholders(
                target_text,
                &tags.target_parts,
                &tags.target_tag_map,
            )?;
            data.target = Some(target.text);
            tags.target_tag_map = target.tag_map;
            tags.target_parts = target.parts;
        }
        data.tags = Some(tags);
    }

    for (_, target) in &mut data.targets {
        let Some(mut tags) = target.tags.take() else {
            continue;
        };
        let Some(text) = target.text.as_deref() else {
            if !tags.tag_map.is_empty() || !tags.parts.is_empty() {
                return Err(PlaceholderError::PartsTextMismatch);
            }
            target.tags = Some(tags);
            continue;
        };
        let resolved = resolve_segment_placeholders(text, &tags.parts, &tags.tag_map)?;
        target.text = Some(resolved.text);
        tags.tag_map = resolved.tag_map;
        tags.parts = resolved.parts;
        target.tags = Some(tags);
    }
    Ok(data)
}

/// Retain generic projection markers as literal text throughout a complete
/// unit, removing only the projection metadata that would make an exporter
/// interpret those markers as native codes.
pub fn literalize_data_placeholders(mut data: Data) -> Result<Data, PlaceholderError> {
    if let Some(mut tags) = data.tags.take() {
        let source = literalize_segment_placeholders(
            &data.source,
            &tags.source_parts,
            &tags.source_tag_map,
        )?;
        data.source = source.text;
        tags.source_tag_map = source.tag_map;
        tags.source_parts = source.parts;

        if let Some(target_text) = data.target.as_deref() {
            let target = literalize_segment_placeholders(
                target_text,
                &tags.target_parts,
                &tags.target_tag_map,
            )?;
            data.target = Some(target.text);
            tags.target_tag_map = target.tag_map;
            tags.target_parts = target.parts;
        }
        data.tags = Some(tags);
    }

    for (_, target) in &mut data.targets {
        let Some(mut tags) = target.tags.take() else {
            continue;
        };
        let Some(text) = target.text.as_deref() else {
            if !tags.tag_map.is_empty() || !tags.parts.is_empty() {
                return Err(PlaceholderError::PartsTextMismatch);
            }
            target.tags = Some(tags);
            continue;
        };
        let literal = literalize_segment_placeholders(text, &tags.parts, &tags.tag_map)?;
        target.text = Some(literal.text);
        tags.tag_map = literal.tag_map;
        tags.parts = literal.parts;
        target.tags = Some(tags);
    }
    Ok(data)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProjectionKind {
    Runtime,
    Inline,
}

struct ProjectionMetadata<'a> {
    kind: ProjectionKind,
    sequence: usize,
    token: &'a str,
}

fn has_projection_attributes(tag_map: &[(String, TieData)]) -> bool {
    tag_map.iter().any(|(_, tag)| {
        tag.attributes
            .iter()
            .any(|(name, _)| is_projection_attribute(name))
    })
}

const fn preserves_incomplete_native_structure(error: &PlaceholderError) -> bool {
    matches!(
        error,
        PlaceholderError::DuplicateTagMapKey { .. }
            | PlaceholderError::DuplicateCodeReference { .. }
            | PlaceholderError::DanglingCodeReference { .. }
            | PlaceholderError::UnreferencedTag { .. }
            | PlaceholderError::PartsTextMismatch
    )
}

fn projection_metadata<'a>(
    key: &str,
    tag: &'a TieData,
) -> Result<Option<ProjectionMetadata<'a>>, PlaceholderError> {
    let mut kind = None;
    let mut sequence = None;
    let mut token = None;
    let mut has_projection_attribute = false;
    for (name, value) in &tag.attributes {
        if !is_projection_attribute(name) {
            continue;
        }
        has_projection_attribute = true;
        match name.as_str() {
            ATTRIBUTE_KIND if kind.is_none() => {
                kind = match value.as_str() {
                    KIND_RUNTIME => Some(ProjectionKind::Runtime),
                    KIND_INLINE => Some(ProjectionKind::Inline),
                    _ => {
                        return Err(PlaceholderError::ProjectionMetadataConflict {
                            key: key.to_owned(),
                        });
                    }
                };
            }
            ATTRIBUTE_SEQUENCE if sequence.is_none() => {
                sequence = Some(value.parse::<usize>().map_err(|_| {
                    PlaceholderError::ProjectionMetadataConflict {
                        key: key.to_owned(),
                    }
                })?);
            }
            ATTRIBUTE_TOKEN if token.is_none() => token = Some(value.as_str()),
            ATTRIBUTE_KIND | ATTRIBUTE_SEQUENCE | ATTRIBUTE_TOKEN => {
                return Err(PlaceholderError::ProjectionMetadataConflict {
                    key: key.to_owned(),
                });
            }
            _ => {}
        }
    }
    if !has_projection_attribute {
        return Ok(None);
    }
    let (Some(kind), Some(sequence), Some(token)) = (kind, sequence, token) else {
        return Err(PlaceholderError::ProjectionMetadataConflict {
            key: key.to_owned(),
        });
    };
    Ok(Some(ProjectionMetadata {
        kind,
        sequence,
        token,
    }))
}

fn parse_projection_token(token: &str) -> Result<(&str, usize), PlaceholderError> {
    let body = token
        .strip_prefix('{')
        .and_then(|value| value.strip_suffix('}'))
        .ok_or_else(|| PlaceholderError::InvalidProjectionToken {
            token: token.to_owned(),
        })?;
    let (prefix, sequence) =
        body.rsplit_once("_P")
            .ok_or_else(|| PlaceholderError::InvalidProjectionToken {
                token: token.to_owned(),
            })?;
    if prefix.is_empty() || !prefix.starts_with("LOKIT") || sequence.starts_with('0') {
        return Err(PlaceholderError::InvalidProjectionToken {
            token: token.to_owned(),
        });
    }
    let sequence = sequence
        .parse::<usize>()
        .ok()
        .filter(|value| *value > 0)
        .ok_or_else(|| PlaceholderError::InvalidProjectionToken {
            token: token.to_owned(),
        })?;
    Ok((prefix, sequence))
}

fn push_resolved_text(text: &mut String, parts: &mut Vec<SegmentPart>, value: &str) {
    if value.is_empty() {
        return;
    }
    text.push_str(value);
    push_text_part(parts, value);
}

fn push_text_part(parts: &mut Vec<SegmentPart>, value: &str) {
    if value.is_empty() {
        return;
    }
    if let Some(SegmentPart::Text(previous)) = parts.last_mut() {
        previous.value.push_str(value);
    } else {
        parts.push(SegmentPart::Text(TextPart::new(value)));
    }
}

struct SegmentProjectionBuilder<'a> {
    rendered: String,
    parts: Vec<SegmentPart>,
    tag_map: Vec<(String, TieData)>,
    tag_indices: HashMap<String, usize>,
    used_ids: HashSet<String>,
    token_prefix: String,
    options: &'a PlaceholderProjectionOptions,
    implicit_counts: Vec<(PlaceholderFamily, usize)>,
    sequence: usize,
    plain_offset: usize,
    source_hash: u64,
}

impl<'a> SegmentProjectionBuilder<'a> {
    fn new(
        text: &str,
        tag_map: &[(String, TieData)],
        options: &'a PlaceholderProjectionOptions,
        token_prefix: String,
    ) -> Result<Self, PlaceholderError> {
        let mut tag_indices = HashMap::with_capacity(tag_map.len());
        let mut used_ids = HashSet::with_capacity(tag_map.len());
        for (index, (key, _)) in tag_map.iter().enumerate() {
            if tag_indices.insert(key.clone(), index).is_some() {
                return Err(PlaceholderError::DuplicateTagMapKey { key: key.clone() });
            }
            used_ids.insert(key.clone());
        }
        Ok(Self {
            rendered: String::with_capacity(text.len().saturating_add(tag_map.len() * 12)),
            parts: Vec::with_capacity(tag_map.len().saturating_mul(2).saturating_add(1)),
            tag_map: tag_map.to_vec(),
            tag_indices,
            used_ids,
            token_prefix,
            options,
            implicit_counts: Vec::new(),
            sequence: 0,
            plain_offset: 0,
            source_hash: fnv1a(text.as_bytes()),
        })
    }

    fn project_text(&mut self, value: &str) -> Result<(), PlaceholderError> {
        if !self.options.runtime_placeholders {
            self.push_text(value);
            self.plain_offset = self.plain_offset.saturating_add(value.len());
            return Ok(());
        }
        let mut analysis = detect_placeholders(value, &self.options.detection)?;
        let mut previous = 0;
        for occurrence in &mut analysis.occurrences {
            self.next_sequence()?;
            if occurrence.key_range.is_none() {
                occurrence.key = self.next_implicit_key(occurrence.syntax.family());
            }
            self.push_text(&value[previous..occurrence.range.start]);
            let token = self.token();
            let id = self.runtime_id()?;
            self.rendered.push_str(&token);
            self.parts.push(SegmentPart::Code(CodePart::new(&id)));

            let mut tag = TieData::new(&id, TieType::PlaceholderStandalone);
            tag.attributes = vec![
                (ATTRIBUTE_KIND.to_owned(), KIND_RUNTIME.to_owned()),
                (
                    ATTRIBUTE_SYNTAX.to_owned(),
                    occurrence.syntax.as_str().to_owned(),
                ),
                (ATTRIBUTE_KEY.to_owned(), occurrence.key.clone()),
                (
                    ATTRIBUTE_ROLE.to_owned(),
                    occurrence.role.as_str().to_owned(),
                ),
                (
                    ATTRIBUTE_VALUE_TYPE.to_owned(),
                    occurrence.value_type.as_str().to_owned(),
                ),
                (ATTRIBUTE_SEQUENCE.to_owned(), self.sequence.to_string()),
                (ATTRIBUTE_TOKEN.to_owned(), token),
            ];
            tag.position =
                i64::try_from(self.plain_offset + occurrence.range.start).unwrap_or(i64::MAX);
            tag.order = i64::try_from(self.sequence - 1).unwrap_or(i64::MAX);
            tag.original_text = Some(occurrence.original_text.clone());
            self.used_ids.insert(id.clone());
            self.tag_map.push((id, tag));
            previous = occurrence.range.end;
        }
        self.push_text(&value[previous..]);
        self.plain_offset = self.plain_offset.saturating_add(value.len());
        Ok(())
    }

    fn project_code(&mut self, part: &CodePart) -> Result<(), PlaceholderError> {
        if self.options.inline_placeholders {
            self.next_sequence()?;
            let token = self.token();
            let index = *self.tag_indices.get(&part.r#ref).ok_or_else(|| {
                PlaceholderError::DanglingCodeReference {
                    reference: part.r#ref.clone(),
                }
            })?;
            let tag = &mut self.tag_map[index].1;
            tag.attributes.extend([
                (ATTRIBUTE_KIND.to_owned(), KIND_INLINE.to_owned()),
                (ATTRIBUTE_SEQUENCE.to_owned(), self.sequence.to_string()),
                (ATTRIBUTE_TOKEN.to_owned(), token.clone()),
            ]);
            self.rendered.push_str(&token);
        }
        self.parts.push(SegmentPart::Code(part.clone()));
        Ok(())
    }

    fn next_sequence(&mut self) -> Result<(), PlaceholderError> {
        if self.sequence >= self.options.detection.limits.max_occurrences {
            return Err(PlaceholderError::OccurrenceLimitExceeded {
                limit: self.options.detection.limits.max_occurrences,
            });
        }
        self.sequence += 1;
        Ok(())
    }

    fn next_implicit_key(&mut self, family: PlaceholderFamily) -> String {
        let family_index = if let Some(index) = self
            .implicit_counts
            .iter()
            .position(|(candidate, _)| *candidate == family)
        {
            index
        } else {
            self.implicit_counts.push((family, 0));
            self.implicit_counts.len() - 1
        };
        let count = &mut self.implicit_counts[family_index].1;
        let key = format!("@{count}");
        *count += 1;
        key
    }

    fn token(&self) -> String {
        format!("{{{}_P{}}}", self.token_prefix, self.sequence)
    }

    fn runtime_id(&self) -> Result<String, PlaceholderError> {
        let preferred = format!("lokit-ph-{}", self.sequence);
        if !self.used_ids.contains(&preferred) {
            return Ok(preferred);
        }
        let base = format!("lokit-{:016x}-ph-{}", self.source_hash, self.sequence);
        if !self.used_ids.contains(&base) {
            return Ok(base);
        }
        for suffix in 2..=self.used_ids.len().saturating_add(2) {
            let candidate = format!("{base}-{suffix}");
            if !self.used_ids.contains(&candidate) {
                return Ok(candidate);
            }
        }
        Err(PlaceholderError::TokenNamespaceExhausted)
    }

    fn push_text(&mut self, value: &str) {
        if value.is_empty() {
            return;
        }
        self.rendered.push_str(value);
        self.parts
            .push(SegmentPart::Text(TextPart::new(value.to_owned())));
    }
}

fn is_projection_attribute(name: &str) -> bool {
    matches!(
        name,
        ATTRIBUTE_KIND
            | ATTRIBUTE_KEY
            | ATTRIBUTE_ROLE
            | ATTRIBUTE_SEQUENCE
            | ATTRIBUTE_SYNTAX
            | ATTRIBUTE_TOKEN
            | ATTRIBUTE_VALUE_TYPE
    )
}

pub fn canonicalize_placeholders(
    text: &str,
    options: &DetectionOptions,
) -> Result<CanonicalPlaceholderText, PlaceholderError> {
    let analysis = detect_placeholders(text, options)?;
    Ok(canonicalize_analysis(text, &analysis))
}

pub fn reform_placeholders(
    candidate_source: &str,
    candidate_target: &str,
    query_source: &str,
    options: &DetectionOptions,
) -> Result<ReformedTarget, PlaceholderError> {
    let candidate_analysis = detect_placeholders(candidate_source, options)?;
    let query_analysis = detect_placeholders(query_source, options)?;
    let target_analysis = detect_placeholders(candidate_target, options)?;
    let candidate_canonical = canonicalize_analysis(candidate_source, &candidate_analysis);
    let query_canonical = canonicalize_analysis(query_source, &query_analysis);
    if candidate_canonical.signature != query_canonical.signature {
        return Err(PlaceholderError::IncompatibleSourceSignatures {
            candidate: candidate_canonical.signature,
            query: query_canonical.signature,
        });
    }

    let candidate_slots = assign_slots(&candidate_analysis.occurrences);
    let query_slots = assign_slots(&query_analysis.occurrences);
    let slot_count = candidate_slots
        .iter()
        .copied()
        .max()
        .map_or(0, |slot| slot + 1);
    let mut slot_replacements = vec![None; slot_count];
    for (occurrence, slot) in query_analysis
        .occurrences
        .iter()
        .zip(query_slots.iter().copied())
    {
        if let Some(replacement) = slot_replacements.get_mut(slot) {
            replacement.get_or_insert(occurrence);
        }
    }

    let mut candidate_by_identity = HashMap::with_capacity(candidate_analysis.occurrences.len());
    for (index, source) in candidate_analysis.occurrences.iter().enumerate() {
        candidate_by_identity
            .entry((source.syntax.family(), source.key.as_str()))
            .or_insert((index, candidate_slots[index]));
    }

    let mut output = String::with_capacity(candidate_target.len());
    let mut previous = 0;
    let mut changed = false;
    for target in &target_analysis.occurrences {
        let Some(&(source_index, slot)) =
            candidate_by_identity.get(&(target.syntax.family(), target.key.as_str()))
        else {
            return Err(PlaceholderError::UnmappedTargetPlaceholder {
                key: target.key.clone(),
                syntax: target.syntax,
            });
        };
        let source = &candidate_analysis.occurrences[source_index];
        if source.role != target.role || !source.value_type.compatible_with(target.value_type) {
            return Err(PlaceholderError::IncompatibleTargetPlaceholder {
                key: target.key.clone(),
                syntax: target.syntax,
            });
        }
        let replacement = slot_replacements
            .get(slot)
            .and_then(|occurrence| *occurrence)
            .expect("compatible signatures provide every slot");

        output.push_str(&candidate_target[previous..target.range.start]);
        let raw = &candidate_target[target.range.clone()];
        match (&target.key_range, &replacement.key_range) {
            (Some(target_key), Some(_)) if target.key != replacement.key => {
                let relative_start = target_key.start - target.range.start;
                let relative_end = target_key.end - target.range.start;
                output.push_str(&raw[..relative_start]);
                output.push_str(&replacement.key);
                output.push_str(&raw[relative_end..]);
                changed = true;
            }
            _ => output.push_str(raw),
        }
        previous = target.range.end;
    }
    output.push_str(&candidate_target[previous..]);
    Ok(ReformedTarget {
        text: output,
        changed,
        signature: query_canonical.signature,
    })
}

fn canonicalize_analysis(text: &str, analysis: &PlaceholderAnalysis) -> CanonicalPlaceholderText {
    let slots = assign_slots(&analysis.occurrences);
    let mut canonical = String::with_capacity(text.len());
    let mut signature = String::from("v1|");
    let mut previous = 0;
    for (occurrence, slot) in analysis.occurrences.iter().zip(slots) {
        append_canonical_literal(&mut canonical, &text[previous..occurrence.range.start]);
        canonical.push(CANONICAL_SEPARATOR);
        canonical.push_str("lokit:");
        canonical.push_str(occurrence.syntax.family().as_str());
        canonical.push(':');
        canonical.push_str(&slot.to_string());
        canonical.push(':');
        canonical.push_str(occurrence.role.as_str());
        canonical.push(':');
        canonical.push_str(occurrence.value_type.as_str());
        canonical.push(CANONICAL_SEPARATOR);

        if signature.len() > 3 {
            signature.push(';');
        }
        signature.push_str(occurrence.syntax.family().as_str());
        signature.push(':');
        signature.push_str(&slot.to_string());
        signature.push(':');
        signature.push_str(occurrence.role.as_str());
        signature.push(':');
        signature.push_str(occurrence.value_type.as_str());
        previous = occurrence.range.end;
    }
    append_canonical_literal(&mut canonical, &text[previous..]);
    CanonicalPlaceholderText {
        text: canonical,
        signature,
    }
}

fn append_canonical_literal(output: &mut String, value: &str) {
    for character in value.chars() {
        output.push(character);
        if character == CANONICAL_SEPARATOR {
            output.push(character);
        }
    }
}

fn assign_slots(occurrences: &[PlaceholderOccurrence]) -> Vec<usize> {
    let mut identities = HashMap::<(PlaceholderFamily, &str), usize>::new();
    let mut slots = Vec::with_capacity(occurrences.len());
    for occurrence in occurrences {
        let identity = (occurrence.syntax.family(), occurrence.key.as_str());
        let slot = if let Some(slot) = identities.get(&identity) {
            *slot
        } else {
            let slot = identities.len();
            identities.insert(identity, slot);
            slot
        };
        slots.push(slot);
    }
    slots
}

fn collision_free_prefix(text: &str) -> String {
    if !text.contains("{LOKIT_P") {
        return "LOKIT".to_owned();
    }
    let hash = fnv1a(text.as_bytes());
    let base = format!("LOKIT_{hash:016X}");
    if !text.contains(&format!("{{{base}_P")) {
        return base;
    }
    for suffix in 2..=1024 {
        let candidate = format!("{base}_{suffix}");
        if !text.contains(&format!("{{{candidate}_P")) {
            return candidate;
        }
    }
    // A collision here requires over a thousand deliberately forged prefixes.
    // The suffix remains deterministic and is still bounded by input length.
    format!("{base}_{}", text.len())
}

const fn fnv1a(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf29ce484222325_u64;
    let mut index = 0;
    while index < bytes.len() {
        hash ^= bytes[index] as u64;
        hash = hash.wrapping_mul(0x100000001b3);
        index += 1;
    }
    hash
}

fn parse_braced_at(
    text: &str,
    start: usize,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    if text.as_bytes().get(start + 1) == Some(&b'{') {
        if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Mustache, |syntax| {
            parse_mustache(text, start, syntax, options)
        })? {
            return Ok(Some(parsed));
        }
        return Ok(None);
    }

    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Message2, |syntax| {
        parse_message2(text, start, syntax, options)
    })? {
        return Ok(Some(parsed));
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Fluent, |syntax| {
        parse_fluent(text, start, syntax, options)
    })? {
        return Ok(Some(parsed));
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Message, |syntax| {
        parse_message(text, start, syntax, options)
    })? {
        return Ok(Some(parsed));
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Brace, |syntax| {
        parse_brace_field(text, start, syntax, options)
    })? {
        return Ok(Some(parsed));
    }
    Ok(None)
}

fn parse_dollar_at(
    text: &str,
    start: usize,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::EcmaScript, |syntax| {
        parse_dollar_expression(text, start, syntax, false, options)
    })? {
        return Ok(Some(parsed));
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Shell, |syntax| {
        parse_shell_reference(text, start, syntax, options)
    })? {
        return Ok(Some(parsed));
    }
    Ok(None)
}

fn parse_percent_at(
    text: &str,
    start: usize,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let bytes = text.as_bytes();
    if bytes.get(start + 1) == Some(&b'%') {
        return Ok(None);
    }
    if options.auto_detect && looks_like_uri_percent_escape(text, start) {
        return Ok(None);
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Printf, |syntax| {
        parse_printf(text, start, syntax, options.auto_detect)
    })? {
        return Ok(Some(parsed));
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::ObjectPascal, |syntax| {
        Ok(parse_object_pascal(text, start, syntax))
    })? {
        return Ok(Some(parsed));
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Compiler, |syntax| {
        Ok(parse_compiler_internal(text, start, syntax))
    })? {
        return Ok(Some(parsed));
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Boost, |syntax| {
        Ok(parse_boost(text, start, syntax))
    })? {
        return Ok(Some(parsed));
    }
    if let Some(parsed) = parse_syntax_family(options, PlaceholderFamily::Qt, |syntax| {
        Ok(parse_qt(text, start, syntax, options.auto_detect))
    })? {
        return Ok(Some(parsed));
    }
    Ok(None)
}

fn parse_tilde_at(
    text: &str,
    start: usize,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    parse_syntax_family(options, PlaceholderFamily::Tilde, |syntax| {
        Ok(parse_tilde(text, start, syntax))
    })
}

fn parse_swift_at(
    text: &str,
    start: usize,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    parse_syntax_family(options, PlaceholderFamily::Swift, |syntax| {
        parse_swift(text, start, syntax, options)
    })
}

fn parse_swift(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let Some(end) = balanced_end(text, start + 1, b'(', b')', options)? else {
        return Ok(None);
    };
    let content = trim_range(text, start + 2..end - 1);
    if content.is_empty() {
        return Ok(None);
    }
    Ok(Some(ParsedOccurrence {
        end,
        key: text[content.clone()].to_owned(),
        key_range: Some(content),
        role: PlaceholderRole::Value,
        value_type: PlaceholderValueType::Any,
        syntax,
    }))
}

fn parse_syntax_family(
    options: &DetectionOptions,
    family: PlaceholderFamily,
    mut parser: impl FnMut(PlaceholderSyntax) -> Result<Option<ParsedOccurrence>, PlaceholderError>,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    for syntax in options.explicit_syntaxes_for_family(family) {
        if let Some(parsed) = parser(syntax)? {
            return Ok(Some(parsed));
        }
    }
    if let Some(syntax) = options.auto_syntax(family) {
        if !options.syntaxes.contains(&syntax) {
            return parser(syntax);
        }
    }
    Ok(None)
}

fn parse_mustache(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let triple = text.as_bytes().get(start + 2) == Some(&b'{');
    let close = if triple { "}}}" } else { "}}" };
    let content_start = start + if triple { 3 } else { 2 };
    let Some(relative_end) = text[content_start..].find(close) else {
        return Ok(None);
    };
    let end = content_start + relative_end + close.len();
    check_placeholder_span(start, end, options)?;
    let mut key_range = trim_range(text, content_start..content_start + relative_end);
    if key_range.is_empty() {
        return Ok(None);
    }
    let first = text.as_bytes()[key_range.start];
    if first == b'!' {
        return Ok(None);
    }
    let role = if matches!(first, b'#' | b'/' | b'^' | b'>') {
        key_range.start += 1;
        key_range = trim_range(text, key_range);
        PlaceholderRole::Markup
    } else {
        if first == b'&' {
            key_range.start += 1;
            key_range = trim_range(text, key_range);
        }
        PlaceholderRole::Value
    };
    if !valid_path(&text[key_range.clone()]) {
        return Ok(None);
    }
    Ok(Some(ParsedOccurrence {
        end,
        key: text[key_range.clone()].to_owned(),
        key_range: Some(key_range),
        role,
        value_type: PlaceholderValueType::Any,
        syntax,
    }))
}

fn parse_fluent(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let Some(end) = balanced_end(text, start, b'{', b'}', options)? else {
        return Ok(None);
    };
    let mut variable_start = start + 1;
    while text
        .as_bytes()
        .get(variable_start)
        .is_some_and(u8::is_ascii_whitespace)
    {
        variable_start += 1;
    }
    if text.as_bytes().get(variable_start) != Some(&b'$') {
        return Ok(None);
    }
    let key_start = variable_start + 1;
    let content = &text[key_start..end - 1];
    let key_length = content
        .char_indices()
        .take_while(|(_, character)| is_identifier_character(*character))
        .last()
        .map_or(0, |(index, character)| index + character.len_utf8());
    if key_length == 0 {
        return Ok(None);
    }
    let key_range = key_start..key_start + key_length;
    let role = if content[key_length..].contains("->") {
        PlaceholderRole::Selector
    } else {
        PlaceholderRole::Value
    };
    Ok(Some(ParsedOccurrence {
        end,
        key: text[key_range.clone()].to_owned(),
        key_range: Some(key_range),
        role,
        value_type: PlaceholderValueType::Any,
        syntax,
    }))
}

fn parse_message2(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let first = *text.as_bytes().get(start + 1).unwrap_or(&0);
    if !matches!(first, b'$' | b'#' | b'/') {
        return Ok(None);
    }
    let Some(end) = balanced_end(text, start, b'{', b'}', options)? else {
        return Ok(None);
    };
    let role = if matches!(first, b'#' | b'/') {
        PlaceholderRole::Markup
    } else {
        PlaceholderRole::Value
    };
    let key_start = start + 2;
    let key_length = text[key_start..end - 1]
        .char_indices()
        .take_while(|(_, character)| is_identifier_character(*character))
        .last()
        .map_or(0, |(index, character)| index + character.len_utf8());
    if key_length == 0 {
        return Ok(None);
    }
    let key_range = key_start..key_start + key_length;
    let value_type = message_value_type(&text[key_range.end..end - 1]);
    Ok(Some(ParsedOccurrence {
        end,
        key: text[key_range.clone()].to_owned(),
        key_range: Some(key_range),
        role,
        value_type,
        syntax,
    }))
}

fn parse_message(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let Some(end) = balanced_end(text, start, b'{', b'}', options)? else {
        return Ok(None);
    };
    let inner = start + 1..end - 1;
    let separator = top_level_separator(text, inner.clone(), b',');
    let key_range = trim_range(text, start + 1..separator.unwrap_or(end - 1));
    if key_range.is_empty() || !valid_path(&text[key_range.clone()]) {
        return Ok(None);
    }
    let remainder = separator.map_or("", |index| &text[index + 1..end - 1]);
    let value_type = message_value_type(remainder);
    let role = if remainder.trim_start().starts_with("plural")
        || remainder.trim_start().starts_with("select")
        || remainder.trim_start().starts_with("selectordinal")
    {
        PlaceholderRole::Selector
    } else {
        PlaceholderRole::Value
    };
    Ok(Some(ParsedOccurrence {
        end,
        key: text[key_range.clone()].to_owned(),
        key_range: Some(key_range),
        role,
        value_type,
        syntax,
    }))
}

fn parse_brace_field(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let Some(end) = balanced_end(text, start, b'{', b'}', options)? else {
        return Ok(None);
    };
    let inner = start + 1..end - 1;
    let mut field_end = inner.end;
    for delimiter in [b'!', b':', b','] {
        if let Some(index) = top_level_separator(text, inner.clone(), delimiter) {
            field_end = field_end.min(index);
        }
    }
    let key_range = trim_range(text, inner.start..field_end);
    let key = &text[key_range.clone()];
    if !key.is_empty() && !valid_brace_field(key) {
        return Ok(None);
    }
    if key.is_empty()
        && !matches!(
            syntax,
            PlaceholderSyntax::PythonBrace
                | PlaceholderSyntax::RustFormat
                | PlaceholderSyntax::CxxStdFormat
        )
    {
        return Ok(None);
    }
    let value_type = brace_value_type(&text[field_end..end - 1]);
    Ok(Some(ParsedOccurrence {
        end,
        key: key.to_owned(),
        key_range: (!key.is_empty()).then_some(key_range),
        role: PlaceholderRole::Value,
        value_type,
        syntax,
    }))
}

fn parse_dollar_expression(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    allow_shell_operators: bool,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let brace_start = start + 1;
    let Some(end) = balanced_end(text, brace_start, b'{', b'}', options)? else {
        return Ok(None);
    };
    let content_range = trim_range(text, start + 2..end - 1);
    if content_range.is_empty() {
        return Ok(None);
    }
    let content = &text[content_range.clone()];
    let key_length = content
        .char_indices()
        .take_while(|(_, character)| is_identifier_character(*character) || *character == '.')
        .last()
        .map_or(0, |(index, character)| index + character.len_utf8());
    if key_length == 0 {
        return Ok(None);
    }
    if !allow_shell_operators && !content[key_length..].trim().is_empty() {
        return Ok(None);
    }
    let key_range = content_range.start..content_range.start + key_length;
    Ok(Some(ParsedOccurrence {
        end,
        key: text[key_range.clone()].to_owned(),
        key_range: Some(key_range),
        role: PlaceholderRole::Value,
        value_type: PlaceholderValueType::Any,
        syntax,
    }))
}

fn parse_shell_reference(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    options: &DetectionOptions,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    if text.as_bytes().get(start + 1) == Some(&b'{') {
        return parse_dollar_expression(text, start, syntax, true, options);
    }
    let bytes = text.as_bytes();
    let key_start = start + 1;
    let Some(first) = bytes.get(key_start).copied() else {
        return Ok(None);
    };
    if !first.is_ascii_alphabetic() && first != b'_' {
        return Ok(None);
    }
    let mut cursor = key_start + 1;
    while bytes
        .get(cursor)
        .is_some_and(|value| value.is_ascii_alphanumeric() || *value == b'_')
    {
        cursor += 1;
    }
    let key_range = key_start..cursor;
    Ok(Some(ParsedOccurrence {
        end: cursor,
        key: text[key_range.clone()].to_owned(),
        key_range: Some(key_range),
        role: PlaceholderRole::Value,
        value_type: PlaceholderValueType::Any,
        syntax,
    }))
}

fn parse_printf(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    conservative: bool,
) -> Result<Option<ParsedOccurrence>, PlaceholderError> {
    let bytes = text.as_bytes();
    let mut cursor = start + 1;
    if cursor >= bytes.len() {
        return Ok(None);
    }

    if bytes[cursor] == b'{' && syntax == PlaceholderSyntax::RubyFormat {
        if let Some(close) = text[cursor + 1..].find('}') {
            let key_range = cursor + 1..cursor + 1 + close;
            if valid_path(&text[key_range.clone()]) {
                return Ok(Some(ParsedOccurrence {
                    end: key_range.end + 1,
                    key: text[key_range.clone()].to_owned(),
                    key_range: Some(key_range),
                    role: PlaceholderRole::Value,
                    value_type: PlaceholderValueType::Any,
                    syntax,
                }));
            }
        }
    }

    let mut key = String::new();
    let mut key_range = None;
    if bytes[cursor] == b'(' && syntax == PlaceholderSyntax::PythonPercent {
        let Some(relative) = text[cursor + 1..].find(')') else {
            return Ok(None);
        };
        let range = cursor + 1..cursor + 1 + relative;
        if !valid_path(&text[range.clone()]) {
            return Ok(None);
        }
        key = text[range.clone()].to_owned();
        key_range = Some(range);
        cursor += relative + 2;
    } else if bytes[cursor] == b'<' && syntax == PlaceholderSyntax::RubyFormat {
        let Some(relative) = text[cursor + 1..].find('>') else {
            return Ok(None);
        };
        let range = cursor + 1..cursor + 1 + relative;
        if !valid_path(&text[range.clone()]) {
            return Ok(None);
        }
        key = text[range.clone()].to_owned();
        key_range = Some(range);
        cursor += relative + 2;
    } else if bytes[cursor] == b'[' && syntax == PlaceholderSyntax::GoFormat {
        let Some(relative) = text[cursor + 1..].find(']') else {
            return Ok(None);
        };
        let range = cursor + 1..cursor + 1 + relative;
        if !text[range.clone()]
            .bytes()
            .all(|value| value.is_ascii_digit())
        {
            return Ok(None);
        }
        key = text[range.clone()].to_owned();
        key_range = Some(range);
        cursor += relative + 2;
    } else {
        let digits_start = cursor;
        while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
            cursor += 1;
        }
        if cursor > digits_start && bytes.get(cursor) == Some(&b'$') {
            key = text[digits_start..cursor].to_owned();
            key_range = Some(digits_start..cursor);
            cursor += 1;
        } else {
            cursor = digits_start;
        }
    }

    while let Some(value) = bytes.get(cursor).copied() {
        if matches!(value, b'#' | b'0' | b'-' | b'+' | b'\'' | b'I')
            || (syntax == PlaceholderSyntax::JavaFormatter && value == b'<')
            || (!conservative && value == b' ')
            || value.is_ascii_digit()
            || matches!(value, b'.' | b'*' | b'$')
            || matches!(value, b'h' | b'l' | b'L' | b'j' | b'z')
            || (value == b't'
                && !matches!(
                    syntax,
                    PlaceholderSyntax::JavaFormatter
                        | PlaceholderSyntax::GoFormat
                        | PlaceholderSyntax::LuaPrintf
                        | PlaceholderSyntax::DFormat
                        | PlaceholderSyntax::OcamlPrintf
                ))
        {
            cursor += 1;
            continue;
        }
        break;
    }
    let Some(conversion) = bytes.get(cursor).copied() else {
        return Ok(None);
    };
    if !valid_printf_conversion(conversion, syntax, conservative) {
        return Ok(None);
    }
    cursor += 1;
    let value_type = printf_value_type(conversion);
    if matches!(conversion, b't' | b'T') && bytes.get(cursor).is_some_and(u8::is_ascii_alphabetic) {
        cursor += 1;
    }
    Ok(Some(ParsedOccurrence {
        end: cursor,
        key,
        key_range,
        role: PlaceholderRole::Value,
        value_type,
        syntax,
    }))
}

fn looks_like_uri_percent_escape(text: &str, start: usize) -> bool {
    let bytes = text.as_bytes();
    if !bytes
        .get(start + 1..start + 3)
        .is_some_and(|pair| pair.iter().all(u8::is_ascii_hexdigit))
    {
        return false;
    }
    let token_prefix = text[..start]
        .rsplit(char::is_whitespace)
        .next()
        .unwrap_or_default()
        .trim_start_matches(['\'', '"', '(', '<']);
    token_prefix.contains("://")
        || token_prefix.starts_with('/')
        || token_prefix.contains('?')
        || token_prefix.contains('#')
}

fn parse_object_pascal(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
) -> Option<ParsedOccurrence> {
    let bytes = text.as_bytes();
    let mut cursor = start + 1;
    let digits_start = cursor;
    while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
        cursor += 1;
    }
    let (key, key_range) = if cursor > digits_start && bytes.get(cursor) == Some(&b':') {
        let range = digits_start..cursor;
        cursor += 1;
        (text[range.clone()].to_owned(), Some(range))
    } else {
        cursor = digits_start;
        (String::new(), None)
    };
    if bytes.get(cursor) == Some(&b'-') {
        cursor += 1;
    }
    while bytes
        .get(cursor)
        .is_some_and(|value| value.is_ascii_digit() || matches!(value, b'*' | b'.'))
    {
        cursor += 1;
    }
    let conversion = bytes.get(cursor).copied()?;
    if !matches!(
        conversion,
        b'd' | b'u'
            | b'e'
            | b'E'
            | b'f'
            | b'F'
            | b'g'
            | b'G'
            | b'n'
            | b'm'
            | b'p'
            | b's'
            | b'x'
            | b'X'
    ) {
        return None;
    }
    Some(ParsedOccurrence {
        end: cursor + 1,
        key,
        key_range,
        role: PlaceholderRole::Value,
        value_type: printf_value_type(conversion),
        syntax,
    })
}

fn parse_compiler_internal(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
) -> Option<ParsedOccurrence> {
    let bytes = text.as_bytes();
    let mut cursor = start + 1;
    let conversion = match syntax {
        PlaceholderSyntax::GfcInternal => {
            if bytes.get(cursor) == Some(&b'l') {
                cursor += 1;
            }
            let conversion = bytes.get(cursor).copied()?;
            if !matches!(conversion, b'C' | b'L' | b'c' | b's' | b'i' | b'd' | b'u') {
                return None;
            }
            conversion
        }
        PlaceholderSyntax::GccInternal => {
            if bytes.get(cursor) == Some(&b'l') {
                cursor += 1;
                if bytes.get(cursor) == Some(&b'l') {
                    cursor += 1;
                }
            } else if bytes
                .get(cursor)
                .is_some_and(|value| matches!(value, b'w' | b'z' | b't'))
            {
                cursor += 1;
            }
            for flag in [b'q', b'+', b'#'] {
                if bytes.get(cursor) == Some(&flag) {
                    cursor += 1;
                }
            }
            if text[cursor..].starts_with(".*s") {
                cursor += 2;
            }
            let conversion = bytes.get(cursor).copied()?;
            if !matches!(
                conversion,
                b'<' | b'>'
                    | b'\''
                    | b'r'
                    | b'R'
                    | b'{'
                    | b'}'
                    | b'c'
                    | b's'
                    | b'i'
                    | b'd'
                    | b'o'
                    | b'u'
                    | b'x'
                    | b'f'
                    | b'D'
                    | b'E'
                    | b'F'
                    | b'T'
                    | b'A'
                    | b'H'
                    | b'I'
                    | b'O'
                    | b'P'
                    | b'Q'
                    | b'S'
                    | b'X'
                    | b'V'
                    | b'v'
                    | b'C'
                    | b'L'
                    | b'p'
                    | b'@'
                    | b'e'
                    | b'Z'
            ) {
                return None;
            }
            conversion
        }
        _ => return None,
    };
    let role = if syntax == PlaceholderSyntax::GccInternal
        && matches!(conversion, b'<' | b'>' | b'\'' | b'r' | b'R' | b'{' | b'}')
    {
        PlaceholderRole::Markup
    } else {
        PlaceholderRole::Value
    };
    Some(ParsedOccurrence {
        end: cursor + 1,
        key: String::new(),
        key_range: None,
        role,
        value_type: printf_value_type(conversion),
        syntax,
    })
}

fn parse_boost(text: &str, start: usize, syntax: PlaceholderSyntax) -> Option<ParsedOccurrence> {
    let bytes = text.as_bytes();
    let mut cursor = start + 1;
    let key_start = cursor;
    while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
        cursor += 1;
    }
    if cursor == key_start || bytes.get(cursor) != Some(&b'%') {
        return None;
    }
    let key_range = key_start..cursor;
    Some(ParsedOccurrence {
        end: cursor + 1,
        key: text[key_range.clone()].to_owned(),
        key_range: Some(key_range),
        role: PlaceholderRole::Value,
        value_type: PlaceholderValueType::Any,
        syntax,
    })
}

fn parse_qt(
    text: &str,
    start: usize,
    syntax: PlaceholderSyntax,
    conservative: bool,
) -> Option<ParsedOccurrence> {
    let bytes = text.as_bytes();
    let mut cursor = start + 1;
    if matches!(
        syntax,
        PlaceholderSyntax::Smalltalk | PlaceholderSyntax::Ycp
    ) {
        let value = bytes.get(cursor).copied()?;
        if !matches!(value, b'1'..=b'9') {
            return None;
        }
        let key_range = cursor..cursor + 1;
        return Some(ParsedOccurrence {
            end: cursor + 1,
            key: text[key_range.clone()].to_owned(),
            key_range: Some(key_range),
            role: PlaceholderRole::Value,
            value_type: PlaceholderValueType::Any,
            syntax,
        });
    }
    if bytes.get(cursor) == Some(&b'L') {
        cursor += 1;
    }
    if bytes.get(cursor) == Some(&b'n') {
        if conservative || syntax != PlaceholderSyntax::QtPlural {
            return None;
        }
        return Some(ParsedOccurrence {
            end: cursor + 1,
            key: "n".to_owned(),
            key_range: Some(cursor..cursor + 1),
            role: PlaceholderRole::Selector,
            value_type: PlaceholderValueType::Count,
            syntax,
        });
    }
    let key_start = cursor;
    while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
        cursor += 1;
    }
    if cursor == key_start {
        return None;
    }
    let key_range = key_start..cursor;
    Some(ParsedOccurrence {
        end: cursor,
        key: text[key_range.clone()].to_owned(),
        key_range: Some(key_range),
        role: PlaceholderRole::Value,
        value_type: PlaceholderValueType::Any,
        syntax,
    })
}

fn parse_tilde(text: &str, start: usize, syntax: PlaceholderSyntax) -> Option<ParsedOccurrence> {
    let bytes = text.as_bytes();
    let mut cursor = start + 1;
    if bytes.get(cursor) == Some(&b'~') {
        return None;
    }
    while bytes.get(cursor).is_some_and(|value| {
        value.is_ascii_digit() || matches!(value, b',' | b':' | b'@' | b'+' | b'-' | b'#' | b'\'')
    }) {
        cursor += 1;
    }
    let directive = bytes.get(cursor).copied()?;
    if !directive.is_ascii_alphabetic() && !matches!(directive, b'[' | b'{' | b'<' | b'(') {
        return None;
    }
    Some(ParsedOccurrence {
        end: cursor + 1,
        key: String::new(),
        key_range: None,
        role: PlaceholderRole::Value,
        value_type: tilde_value_type(directive),
        syntax,
    })
}

fn balanced_end(
    text: &str,
    start: usize,
    open: u8,
    close: u8,
    options: &DetectionOptions,
) -> Result<Option<usize>, PlaceholderError> {
    let bytes = text.as_bytes();
    if bytes.get(start) != Some(&open) {
        return Ok(None);
    }
    let mut depth = 0;
    let mut cursor = start;
    let mut quote = None;
    let mut escaped = false;
    while let Some(value) = bytes.get(cursor).copied() {
        if cursor - start > options.limits.max_placeholder_bytes {
            return Err(PlaceholderError::PlaceholderLimitExceeded {
                start,
                limit: options.limits.max_placeholder_bytes,
            });
        }
        if escaped {
            escaped = false;
            cursor += 1;
            continue;
        }
        if value == b'\\' {
            escaped = true;
            cursor += 1;
            continue;
        }
        if let Some(active) = quote {
            if value == active {
                quote = None;
            }
            cursor += 1;
            continue;
        }
        if matches!(value, b'\'' | b'"') {
            quote = Some(value);
            cursor += 1;
            continue;
        }
        if value == open {
            depth += 1;
            if depth > options.limits.max_nesting {
                return Err(PlaceholderError::NestingLimitExceeded {
                    start,
                    limit: options.limits.max_nesting,
                });
            }
        } else if value == close {
            if depth == 0 {
                return Ok(None);
            }
            depth -= 1;
            if depth == 0 {
                return Ok(Some(cursor + 1));
            }
        }
        cursor += 1;
    }
    Ok(None)
}

fn check_placeholder_span(
    start: usize,
    end: usize,
    options: &DetectionOptions,
) -> Result<(), PlaceholderError> {
    if end - start > options.limits.max_placeholder_bytes {
        return Err(PlaceholderError::PlaceholderLimitExceeded {
            start,
            limit: options.limits.max_placeholder_bytes,
        });
    }
    Ok(())
}

fn top_level_separator(text: &str, range: Range<usize>, needle: u8) -> Option<usize> {
    let bytes = text.as_bytes();
    let mut braces = 0_usize;
    let mut brackets = 0_usize;
    let mut parentheses = 0_usize;
    let mut quote = None;
    let mut escaped = false;
    for index in range {
        let value = bytes[index];
        if escaped {
            escaped = false;
            continue;
        }
        if value == b'\\' {
            escaped = true;
            continue;
        }
        if let Some(active) = quote {
            if value == active {
                quote = None;
            }
            continue;
        }
        if matches!(value, b'\'' | b'"') {
            quote = Some(value);
            continue;
        }
        match value {
            b'{' => braces += 1,
            b'}' => braces = braces.saturating_sub(1),
            b'[' => brackets += 1,
            b']' => brackets = brackets.saturating_sub(1),
            b'(' => parentheses += 1,
            b')' => parentheses = parentheses.saturating_sub(1),
            _ if value == needle && braces == 0 && brackets == 0 && parentheses == 0 => {
                return Some(index);
            }
            _ => {}
        }
    }
    None
}

fn trim_range(text: &str, mut range: Range<usize>) -> Range<usize> {
    while range.start < range.end {
        let character = text[range.start..range.end]
            .chars()
            .next()
            .expect("range is not empty");
        if !character.is_whitespace() {
            break;
        }
        range.start += character.len_utf8();
    }
    while range.start < range.end {
        let character = text[range.start..range.end]
            .chars()
            .next_back()
            .expect("range is not empty");
        if !character.is_whitespace() {
            break;
        }
        range.end -= character.len_utf8();
    }
    range
}

fn valid_path(value: &str) -> bool {
    !value.is_empty()
        && value.chars().all(|character| {
            is_identifier_character(character)
                || matches!(character, '.' | '-' | ':' | '/' | '[' | ']' | '\'' | '"')
        })
}

fn valid_brace_field(value: &str) -> bool {
    if value.bytes().all(|byte| byte.is_ascii_digit()) {
        return true;
    }
    valid_path(value)
}

fn is_identifier_character(character: char) -> bool {
    character == '_' || character.is_alphanumeric()
}

fn valid_printf_conversion(conversion: u8, syntax: PlaceholderSyntax, conservative: bool) -> bool {
    if conservative {
        return matches!(
            conversion,
            b'd' | b'i'
                | b'o'
                | b'u'
                | b'x'
                | b'X'
                | b'f'
                | b'F'
                | b'e'
                | b'E'
                | b'g'
                | b'G'
                | b'a'
                | b'A'
                | b'c'
                | b'C'
                | b's'
                | b'S'
                | b'p'
        );
    }
    if conversion == b'n' {
        return matches!(
            syntax,
            PlaceholderSyntax::CPrintf
                | PlaceholderSyntax::ObjectiveCPrintf
                | PlaceholderSyntax::CxxPrintf
        );
    }
    match syntax {
        PlaceholderSyntax::ObjectiveCPrintf => matches!(
            conversion,
            b'd' | b'i'
                | b'o'
                | b'u'
                | b'x'
                | b'X'
                | b'f'
                | b'F'
                | b'e'
                | b'E'
                | b'g'
                | b'G'
                | b'a'
                | b'A'
                | b'c'
                | b'C'
                | b's'
                | b'S'
                | b'p'
                | b'n'
                | b'@'
        ),
        PlaceholderSyntax::JavaScriptPrintf => {
            matches!(
                conversion,
                b'c' | b's' | b'b' | b'd' | b'o' | b'x' | b'X' | b'f' | b'j'
            )
        }
        PlaceholderSyntax::ShellPrintf => matches!(
            conversion,
            b'c' | b's'
                | b'i'
                | b'd'
                | b'u'
                | b'o'
                | b'x'
                | b'X'
                | b'a'
                | b'A'
                | b'e'
                | b'E'
                | b'f'
                | b'F'
                | b'g'
                | b'G'
        ),
        PlaceholderSyntax::Modula2Printf => {
            matches!(conversion, b's' | b'c' | b'd' | b'u' | b'x')
        }
        PlaceholderSyntax::LuaPrintf => matches!(
            conversion,
            b'c' | b'd'
                | b'E'
                | b'e'
                | b'f'
                | b'g'
                | b'G'
                | b'i'
                | b'o'
                | b'q'
                | b's'
                | b'u'
                | b'X'
                | b'x'
        ),
        PlaceholderSyntax::DFormat | PlaceholderSyntax::OcamlPrintf => {
            conversion.is_ascii_alphabetic() || matches!(conversion, b'@')
        }
        PlaceholderSyntax::GoFormat => matches!(
            conversion,
            b'v' | b'T'
                | b't'
                | b'b'
                | b'c'
                | b'd'
                | b'o'
                | b'O'
                | b'x'
                | b'X'
                | b'U'
                | b'e'
                | b'E'
                | b'f'
                | b'F'
                | b'g'
                | b'G'
                | b's'
                | b'q'
                | b'p'
        ),
        _ => matches!(
            conversion,
            b'd' | b'i'
                | b'o'
                | b'u'
                | b'x'
                | b'X'
                | b'f'
                | b'F'
                | b'e'
                | b'E'
                | b'g'
                | b'G'
                | b'a'
                | b'A'
                | b'c'
                | b'C'
                | b's'
                | b'S'
                | b'p'
                | b'v'
                | b'T'
                | b't'
                | b'q'
                | b'b'
        ),
    }
}

fn printf_value_type(conversion: u8) -> PlaceholderValueType {
    match conversion {
        b'd' | b'i' | b'o' | b'u' | b'x' | b'X' => PlaceholderValueType::Integer,
        b'f' | b'F' | b'e' | b'E' | b'g' | b'G' | b'a' | b'A' => PlaceholderValueType::Float,
        b'c' | b'C' => PlaceholderValueType::Character,
        b's' | b'S' | b'@' | b'q' | b'j' => PlaceholderValueType::String,
        b'p' | b'n' => PlaceholderValueType::Pointer,
        b't' | b'T' => PlaceholderValueType::Date,
        _ => PlaceholderValueType::Any,
    }
}

fn brace_value_type(remainder: &str) -> PlaceholderValueType {
    let lowered = remainder.to_ascii_lowercase();
    if lowered.contains('%') || lowered.contains('f') || lowered.contains('e') {
        PlaceholderValueType::Number
    } else if lowered.contains('d') || lowered.contains('x') || lowered.contains('o') {
        PlaceholderValueType::Integer
    } else if lowered.contains('s') {
        PlaceholderValueType::String
    } else {
        PlaceholderValueType::Any
    }
}

fn message_value_type(remainder: &str) -> PlaceholderValueType {
    let lowered = remainder.trim_start().to_ascii_lowercase();
    if lowered.starts_with("number")
        || lowered.starts_with("plural")
        || lowered.starts_with("selectordinal")
    {
        PlaceholderValueType::Number
    } else if lowered.starts_with("date") {
        PlaceholderValueType::Date
    } else if lowered.starts_with("time") {
        PlaceholderValueType::Time
    } else {
        PlaceholderValueType::Any
    }
}

fn tilde_value_type(directive: u8) -> PlaceholderValueType {
    match directive.to_ascii_lowercase() {
        b'd' | b'b' | b'o' | b'x' | b'r' => PlaceholderValueType::Integer,
        b'f' | b'e' | b'g' | b'$' => PlaceholderValueType::Float,
        b'c' => PlaceholderValueType::Character,
        b'a' | b's' | b'w' => PlaceholderValueType::String,
        _ => PlaceholderValueType::Any,
    }
}

fn gettext_syntax(value: &str) -> Option<PlaceholderSyntax> {
    let name = value.strip_suffix("-format").unwrap_or(value);
    match name {
        "c" => Some(PlaceholderSyntax::CPrintf),
        "objc" => Some(PlaceholderSyntax::ObjectiveCPrintf),
        "c++" => Some(PlaceholderSyntax::CxxStdFormat),
        "python" => Some(PlaceholderSyntax::PythonPercent),
        "python-brace" => Some(PlaceholderSyntax::PythonBrace),
        "java" => Some(PlaceholderSyntax::JavaMessageFormat),
        "java-printf" => Some(PlaceholderSyntax::JavaFormatter),
        "csharp" => Some(PlaceholderSyntax::DotNetComposite),
        "javascript" => Some(PlaceholderSyntax::JavaScriptPrintf),
        "scheme" => Some(PlaceholderSyntax::Scheme),
        "lisp" => Some(PlaceholderSyntax::Lisp),
        "elisp" => Some(PlaceholderSyntax::Elisp),
        "librep" => Some(PlaceholderSyntax::Librep),
        "rust" => Some(PlaceholderSyntax::RustFormat),
        "go" => Some(PlaceholderSyntax::GoFormat),
        "ruby" => Some(PlaceholderSyntax::RubyFormat),
        "sh" => Some(PlaceholderSyntax::ShellParameter),
        "sh-printf" => Some(PlaceholderSyntax::ShellPrintf),
        "awk" => Some(PlaceholderSyntax::AwkPrintf),
        "lua" => Some(PlaceholderSyntax::LuaPrintf),
        "object-pascal" => Some(PlaceholderSyntax::ObjectPascal),
        "modula2" | "modula-2" => Some(PlaceholderSyntax::Modula2Printf),
        "d" => Some(PlaceholderSyntax::DFormat),
        "ocaml" => Some(PlaceholderSyntax::OcamlPrintf),
        "smalltalk" => Some(PlaceholderSyntax::Smalltalk),
        "qt" => Some(PlaceholderSyntax::QtArg),
        "qt-plural" => Some(PlaceholderSyntax::QtPlural),
        "kde" => Some(PlaceholderSyntax::Kde),
        "kde-kuit" | "kuit" => Some(PlaceholderSyntax::KdeKuit),
        "boost" => Some(PlaceholderSyntax::Boost),
        "tcl" => Some(PlaceholderSyntax::TclPrintf),
        "perl" => Some(PlaceholderSyntax::PerlPrintf),
        "perl-brace" => Some(PlaceholderSyntax::PerlBrace),
        "php" => Some(PlaceholderSyntax::PhpPrintf),
        "gcc-internal" => Some(PlaceholderSyntax::GccInternal),
        "gfc-internal" => Some(PlaceholderSyntax::GfcInternal),
        "ycp" => Some(PlaceholderSyntax::Ycp),
        _ => None,
    }
}

fn unique_syntaxes(values: impl IntoIterator<Item = PlaceholderSyntax>) -> Vec<PlaceholderSyntax> {
    let mut result = Vec::new();
    for value in values {
        push_unique(&mut result, value);
    }
    result
}

fn push_unique(values: &mut Vec<PlaceholderSyntax>, value: PlaceholderSyntax) {
    if !values.contains(&value) {
        values.push(value);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{validate_unit, Tags};

    fn explicit(syntax: PlaceholderSyntax) -> DetectionOptions {
        DetectionOptions::explicit([syntax])
    }

    fn originals(text: &str, syntax: PlaceholderSyntax) -> Vec<String> {
        detect_placeholders(text, &explicit(syntax))
            .expect("scan succeeds")
            .occurrences
            .into_iter()
            .map(|occurrence| occurrence.original_text)
            .collect()
    }

    #[test]
    fn detects_printf_families_and_escapes() {
        assert_eq!(
            originals("%% %1$*2$.*3$f %@ %s", PlaceholderSyntax::CPrintf),
            ["%1$*2$.*3$f", "%s"]
        );
        assert_eq!(
            originals("%@ %d", PlaceholderSyntax::ObjectiveCPrintf),
            ["%@", "%d"]
        );
        assert_eq!(
            originals("%(name)-10.2f", PlaceholderSyntax::PythonPercent),
            ["%(name)-10.2f"]
        );
        assert_eq!(
            originals("%[2]d %[1]s", PlaceholderSyntax::GoFormat),
            ["%[2]d", "%[1]s"]
        );
        assert_eq!(
            originals("%{name} %<price>.2f", PlaceholderSyntax::RubyFormat),
            ["%{name}", "%<price>.2f"]
        );
    }

    #[test]
    fn detects_all_gettext_runtime_format_families() {
        assert_eq!(
            originals(
                "$name ${other:-fallback}",
                PlaceholderSyntax::ShellParameter
            ),
            ["$name", "${other:-fallback}"]
        );
        assert_eq!(originals("%2$s", PlaceholderSyntax::ShellPrintf), ["%2$s"]);
        assert_eq!(originals("%08d", PlaceholderSyntax::AwkPrintf), ["%08d"]);
        assert_eq!(originals("%q", PlaceholderSyntax::LuaPrintf), ["%q"]);
        assert_eq!(originals("%0:s", PlaceholderSyntax::ObjectPascal), ["%0:s"]);
        assert_eq!(
            originals("%08x", PlaceholderSyntax::Modula2Printf),
            ["%08x"]
        );
        assert_eq!(originals("%2$d", PlaceholderSyntax::DFormat), ["%2$d"]);
        assert_eq!(originals("%2$d", PlaceholderSyntax::OcamlPrintf), ["%2$d"]);
        assert_eq!(originals("%1", PlaceholderSyntax::KdeKuit), ["%1"]);
        assert_eq!(
            originals("%qT %<quoted%>", PlaceholderSyntax::GccInternal),
            ["%qT", "%<", "%>"]
        );
        assert_eq!(
            originals("%L %ld", PlaceholderSyntax::GfcInternal),
            ["%L", "%ld"]
        );
        assert_eq!(originals("%1 %9", PlaceholderSyntax::Ycp), ["%1", "%9"]);
    }

    #[test]
    fn detects_brace_message_and_framework_families() {
        assert_eq!(
            originals("{{literal}} {name!r:>10}", PlaceholderSyntax::PythonBrace),
            ["{name!r:>10}"]
        );
        assert_eq!(
            originals("{} {value:?}", PlaceholderSyntax::RustFormat),
            ["{}", "{value:?}"]
        );
        assert_eq!(
            originals(
                "{count, plural, one {One file} other {{count} files}}",
                PlaceholderSyntax::IcuMessageFormat1,
            ),
            ["{count, plural, one {One file} other {{count} files}}"]
        );
        assert_eq!(
            originals("Hello {0,number}", PlaceholderSyntax::JavaMessageFormat),
            ["{0,number}"]
        );
        assert_eq!(
            originals(
                "{$name :number} {#strong}x{/strong}",
                PlaceholderSyntax::UnicodeMessageFormat2
            ),
            ["{$name :number}", "{#strong}", "{/strong}"]
        );
    }

    #[test]
    fn detects_qt_boost_tilde_and_template_families() {
        assert_eq!(
            originals("%1 %L2 %n", PlaceholderSyntax::QtPlural),
            ["%1", "%L2", "%n"]
        );
        assert_eq!(originals("%1%", PlaceholderSyntax::Boost), ["%1%"]);
        assert_eq!(
            originals("~A ~~ ~10D", PlaceholderSyntax::Lisp),
            ["~A", "~10D"]
        );
        assert_eq!(
            originals("${user.name}", PlaceholderSyntax::EcmaScriptTemplate),
            ["${user.name}"]
        );
        assert_eq!(
            originals("${name:-guest}", PlaceholderSyntax::ShellParameter),
            ["${name:-guest}"]
        );
        assert_eq!(
            originals(r"Hello \(user.name)", PlaceholderSyntax::SwiftInterpolation),
            [r"\(user.name)"]
        );
    }

    #[test]
    fn detects_fluent_mustache_and_handlebars_without_comments() {
        assert_eq!(
            originals(
                "{ $count -> [one] One *[other] Many }",
                PlaceholderSyntax::Fluent
            ),
            ["{ $count -> [one] One *[other] Many }"]
        );
        assert_eq!(
            originals(
                "{{name}} {{{raw}}} {{! ignore}} {{#items}}{{/items}}",
                PlaceholderSyntax::Mustache
            ),
            ["{{name}}", "{{{raw}}}", "{{#items}}", "{{/items}}"]
        );
    }

    #[test]
    fn gettext_hints_honor_positive_and_no_flags() {
        let options =
            DetectionOptions::from_hints([], &["python-format, no-c-format".to_owned()], true);
        assert_eq!(options.syntaxes, [PlaceholderSyntax::PythonPercent]);
        assert!(!options.auto_detect);
        assert!(options
            .disabled_syntaxes
            .contains(&PlaceholderSyntax::CPrintf));
        let analysis = detect_placeholders("%(name)s {ignored}", &options).expect("scan succeeds");
        assert_eq!(analysis.occurrences.len(), 1);
        assert_eq!(analysis.occurrences[0].key, "name");
    }

    #[test]
    fn auto_detection_is_conservative() {
        let analysis = detect_placeholders(
            "100%% {name} {{user}} ${account.id} %1 %n $bare ${x:-fallback}",
            &DetectionOptions::default(),
        )
        .expect("scan succeeds");
        let values = analysis
            .occurrences
            .iter()
            .map(|occurrence| occurrence.original_text.as_str())
            .collect::<Vec<_>>();
        assert_eq!(values, ["{name}", "{{user}}", "${account.id}", "%1"]);

        let uri = detect_placeholders(
            "https://example.test/#:~:text=By%20combining%20sophisticated%20systems",
            &DetectionOptions::default(),
        )
        .expect("URI scanning succeeds");
        assert!(uri.occurrences.is_empty(), "{:?}", uri.occurrences);
        assert_eq!(
            originals("%20c", PlaceholderSyntax::CPrintf),
            ["%20c"],
            "explicit printf selection remains standards-complete"
        );
    }

    #[test]
    fn projection_is_ordered_and_collision_safe() {
        let projected = project_placeholders(
            "Literal {LOKIT_P1}; hello {name}, total %1$d",
            &DetectionOptions::default(),
        )
        .expect("projection succeeds");
        assert_ne!(projected.token_prefix, "LOKIT");
        assert_eq!(projected.tag_map.len(), 3);
        assert!(projected
            .text
            .contains(&format!("{{{}_P1}}", projected.token_prefix)));
        assert_eq!(
            projected.tag_map[0].1.r#type,
            TieType::PlaceholderStandalone
        );
        assert_eq!(
            projected.tag_map[0].1.original_text.as_deref(),
            Some("{LOKIT_P1}")
        );
        assert_eq!(projected.parts.len(), 6);
    }

    #[test]
    fn runtime_and_inline_projection_share_one_reversible_sequence() {
        let mut opening = TieData::new("open", TieType::StrongOpen);
        opening.attributes = vec![("class".to_owned(), "warning".to_owned())];
        opening.pair_id = Some("pair".to_owned());
        opening.original_name = Some("strong".to_owned());
        let mut closing = TieData::new("close", TieType::StrongClose);
        closing.pair_id = Some("pair".to_owned());
        closing.original_name = Some("strong".to_owned());
        let tag_map = vec![
            ("open".to_owned(), opening.clone()),
            ("close".to_owned(), closing.clone()),
        ];
        let parts = vec![
            SegmentPart::Text(TextPart::new("Hello ")),
            SegmentPart::Code(CodePart::new("open")),
            SegmentPart::Text(TextPart::new("{name}")),
            SegmentPart::Code(CodePart::new("close")),
            SegmentPart::Text(TextPart::new("!")),
        ];
        let options = PlaceholderProjectionOptions {
            detection: explicit(PlaceholderSyntax::PythonBrace),
            ..PlaceholderProjectionOptions::default()
        };

        let projected = project_segment_placeholders("Hello {name}!", &parts, &tag_map, &options)
            .expect("valid inline graph projects");

        assert_eq!(projected.text, "Hello {LOKIT_P1}{LOKIT_P2}{LOKIT_P3}!");
        assert_eq!(projected.tag_map.len(), 3);
        assert_eq!(projected.tag_map[0].1.r#type, TieType::StrongOpen);
        assert_eq!(
            projected.tag_map[0]
                .1
                .attributes
                .first()
                .map(|(name, value)| (name.as_str(), value.as_str())),
            Some(("class", "warning"))
        );
        assert_eq!(
            projected.tag_map[2].1.r#type,
            TieType::PlaceholderStandalone
        );
        assert_eq!(
            projected.tag_map[2].1.original_text.as_deref(),
            Some("{name}")
        );
        assert_eq!(
            projected
                .parts
                .iter()
                .filter(|part| matches!(part, SegmentPart::Code(_)))
                .count(),
            3
        );

        let reprojected = project_segment_placeholders(
            &projected.text,
            &projected.parts,
            &projected.tag_map,
            &options,
        )
        .expect("valid projected graph is idempotent");
        assert_eq!(reprojected, projected);
        let resolved =
            resolve_segment_placeholders(&projected.text, &projected.parts, &projected.tag_map)
                .expect("projected graph resolves");
        assert_eq!(resolved.text, "Hello {name}!");
        assert_eq!(resolved.tag_map, tag_map);
        assert_eq!(resolved.parts, parts);

        let literal =
            literalize_segment_placeholders(&projected.text, &projected.parts, &projected.tag_map)
                .expect("projected graph literalizes");
        assert_eq!(literal.text, "Hello {LOKIT_P1}{LOKIT_P2}{LOKIT_P3}!");
        assert!(literal.tag_map.is_empty());
        assert_eq!(
            literal.parts,
            [SegmentPart::Text(TextPart::new(
                "Hello {LOKIT_P1}{LOKIT_P2}{LOKIT_P3}!"
            ))]
        );

        let mut data = Data::new(projected.text.clone());
        data.tags = Some(Tags {
            source_tag_map: projected.tag_map,
            source_parts: projected.parts,
            ..Tags::default()
        });
        assert!(validate_unit(0, "projected", &data).is_empty());
    }

    #[test]
    fn data_projection_stays_in_rust_and_projects_targets_independently() {
        let mut data = Data::new("Hello {name}");
        data.target = Some("Bonjour {name}".to_owned());
        data.targets.push((
            "de".to_owned(),
            crate::TargetData {
                text: Some("Hallo {name}".to_owned()),
                ..crate::TargetData::default()
            },
        ));
        let options = PlaceholderProjectionOptions {
            detection: explicit(PlaceholderSyntax::PythonBrace),
            ..PlaceholderProjectionOptions::default()
        };

        let projected =
            project_data_placeholders(data, &options).expect("complete data graph projects");

        assert_eq!(projected.source, "Hello {LOKIT_P1}");
        assert_eq!(projected.target.as_deref(), Some("Bonjour {LOKIT_P1}"));
        assert_eq!(
            projected.targets[0].1.text.as_deref(),
            Some("Hallo {LOKIT_P1}")
        );
        assert_eq!(
            projected
                .tags
                .as_ref()
                .map(|tags| tags.source_tag_map.len()),
            Some(1)
        );
        assert_eq!(
            projected.targets[0]
                .1
                .tags
                .as_ref()
                .map(|tags| tags.tag_map.len()),
            Some(1)
        );
        assert!(validate_unit(0, "projected", &projected).is_empty());

        let resolved =
            resolve_data_placeholders(projected.clone()).expect("complete graph resolves");
        assert_eq!(resolved.source, "Hello {name}");
        assert_eq!(resolved.target.as_deref(), Some("Bonjour {name}"));
        assert_eq!(resolved.targets[0].1.text.as_deref(), Some("Hallo {name}"));
        assert!(validate_unit(0, "resolved", &resolved).is_empty());

        let literal =
            literalize_data_placeholders(projected.clone()).expect("complete graph literalizes");
        assert_eq!(literal.source, "Hello {LOKIT_P1}");
        assert_eq!(literal.target.as_deref(), Some("Bonjour {LOKIT_P1}"));
        assert_eq!(
            literal.targets[0].1.text.as_deref(),
            Some("Hallo {LOKIT_P1}")
        );
        assert!(validate_unit(0, "literal", &literal).is_empty());
        assert_eq!(
            project_data_placeholders(resolved, &options).expect("resolved graph reprojects"),
            projected
        );

        let mut source_only = Data::new("Hello {name}");
        source_only.tags = Some(Tags {
            target_parts: vec![SegmentPart::Text(TextPart::new("unselected target"))],
            ..Tags::default()
        });
        let source_only = project_data_placeholders(source_only, &options)
            .expect("unselected base-target metadata is preserved");
        assert_eq!(source_only.source, "Hello {LOKIT_P1}");
        assert_eq!(
            source_only
                .tags
                .as_ref()
                .map(|tags| tags.target_parts.as_slice()),
            Some([SegmentPart::Text(TextPart::new("unselected target"))].as_slice())
        );
    }

    #[test]
    fn segment_projection_preserves_incomplete_native_graphs_and_rejects_partial_metadata() {
        let tag = TieData::new("code", TieType::Br);
        let options = PlaceholderProjectionOptions::default();
        let mismatched_parts = vec![SegmentPart::Text(TextPart::new("other"))];
        let mismatched = project_segment_placeholders("text", &mismatched_parts, &[], &options)
            .expect("incomplete native edits remain lossless");
        assert_eq!(mismatched.text, "text");
        assert_eq!(mismatched.parts, mismatched_parts);

        let dangling_parts = vec![
            SegmentPart::Text(TextPart::new("text")),
            SegmentPart::Code(CodePart::new("missing")),
        ];
        let dangling_map = vec![("code".to_owned(), tag.clone())];
        let dangling =
            project_segment_placeholders("text", &dangling_parts, &dangling_map, &options)
                .expect("dangling draft codes remain lossless");
        assert_eq!(dangling.parts, dangling_parts);
        assert_eq!(dangling.tag_map, dangling_map);
        assert_eq!(
            resolve_segment_placeholders("text", &dangling.parts, &dangling.tag_map)
                .expect("unprojected draft resolves as a no-op")
                .parts,
            dangling.parts
        );

        let mut projected_tag = tag;
        projected_tag
            .attributes
            .push((ATTRIBUTE_TOKEN.to_owned(), "{LOKIT_P1}".to_owned()));
        assert!(matches!(
            project_segment_placeholders(
                "text",
                &[
                    SegmentPart::Text(TextPart::new("text")),
                    SegmentPart::Code(CodePart::new("code"))
                ],
                &[("code".to_owned(), projected_tag)],
                &options,
            ),
            Err(PlaceholderError::ProjectionMetadataConflict { .. })
        ));
    }

    #[test]
    fn canonicalization_ignores_names_but_preserves_repeat_graph() {
        let options = explicit(PlaceholderSyntax::PythonBrace);
        let left =
            canonicalize_placeholders("Hello {name}, {name}", &options).expect("canonicalizes");
        let right =
            canonicalize_placeholders("Hello {user}, {user}", &options).expect("canonicalizes");
        let different =
            canonicalize_placeholders("Hello {first}, {last}", &options).expect("canonicalizes");
        assert_eq!(left.text, right.text);
        assert_eq!(left.signature, right.signature);
        assert_ne!(left.signature, different.signature);
    }

    #[test]
    fn reforms_named_reordered_and_repeated_target_placeholders() {
        let result = reform_placeholders(
            "From {origin} to {destination}",
            "De {destination} à {origin}; {origin}",
            "From {start} to {end}",
            &explicit(PlaceholderSyntax::PythonBrace),
        )
        .expect("reformation succeeds");
        assert_eq!(result.text, "De {end} à {start}; {start}");
        assert!(result.changed);
    }

    #[test]
    fn reforms_positional_printf_without_overwriting_format_specifiers() {
        let result = reform_placeholders(
            "From %1$s to %2$s",
            "De %2$20s à %1$s",
            "From %2$s to %1$s",
            &explicit(PlaceholderSyntax::CPrintf),
        )
        .expect("reformation succeeds");
        assert_eq!(result.text, "De %1$20s à %2$s");
    }

    #[test]
    fn reforms_icu_selector_name_and_preserves_translated_branches() {
        let result = reform_placeholders(
            "{count, plural, one {One file} other {Many files}}",
            "{count, plural, one {Un fichier} other {Des fichiers}}",
            "{total, plural, one {One file} other {Many files}}",
            &explicit(PlaceholderSyntax::IcuMessageFormat1),
        )
        .expect("reformation succeeds");
        assert_eq!(
            result.text,
            "{total, plural, one {Un fichier} other {Des fichiers}}"
        );
    }

    #[test]
    fn rejects_incompatible_repeat_graph_and_unmapped_target() {
        let options = explicit(PlaceholderSyntax::PythonBrace);
        assert!(matches!(
            reform_placeholders("{a} {b}", "{a} {b}", "{x} {x}", &options),
            Err(PlaceholderError::IncompatibleSourceSignatures { .. })
        ));
        assert!(matches!(
            reform_placeholders("{a}", "{extra}", "{x}", &options),
            Err(PlaceholderError::UnmappedTargetPlaceholder { .. })
        ));
    }

    #[test]
    fn malformed_inputs_remain_literal_and_limits_are_enforced() {
        assert!(
            detect_placeholders("{unterminated %", &DetectionOptions::default())
                .expect("malformed input is literal")
                .occurrences
                .is_empty()
        );

        let mut options = DetectionOptions::default();
        options.limits.max_input_bytes = 3;
        assert!(matches!(
            detect_placeholders("four", &options),
            Err(PlaceholderError::InputLimitExceeded { .. })
        ));

        let mut options = explicit(PlaceholderSyntax::IcuMessageFormat1);
        options.limits.max_nesting = 2;
        assert!(matches!(
            detect_placeholders("{a, select, x {{nested}}}", &options),
            Err(PlaceholderError::NestingLimitExceeded { .. })
        ));
    }

    #[test]
    fn scanning_preserves_utf8_boundaries() {
        let analysis = detect_placeholders("Héllo {用户} — {{имя}}", &DetectionOptions::default())
            .expect("scan succeeds");
        assert_eq!(analysis.occurrences.len(), 2);
        for occurrence in analysis.occurrences {
            assert_eq!(
                &"Héllo {用户} — {{имя}}"[occurrence.range],
                occurrence.original_text
            );
        }
    }

    #[test]
    fn arbitrary_valid_utf8_never_produces_overlapping_or_invalid_ranges() {
        let alphabet = [
            'a', 'Z', '0', '%', '{', '}', '$', '~', '\\', '(', ')', '[', ']', ':', '.', '*', ' ',
            'é', '用', '🙂', '\u{1f}',
        ];
        let mut state = 0x9e37_79b9_7f4a_7c15_u64;
        for length in 0..256 {
            let mut text = String::new();
            for _ in 0..length {
                state = state
                    .wrapping_mul(6_364_136_223_846_793_005)
                    .wrapping_add(1_442_695_040_888_963_407);
                text.push(alphabet[(state as usize) % alphabet.len()]);
            }
            let analysis = detect_placeholders(&text, &DetectionOptions::default())
                .expect("bounded scanner accepts fuzz text");
            let mut previous_end = 0;
            for occurrence in analysis.occurrences {
                assert!(occurrence.range.start >= previous_end);
                assert!(occurrence.range.end <= text.len());
                assert!(text.is_char_boundary(occurrence.range.start));
                assert!(text.is_char_boundary(occurrence.range.end));
                assert_eq!(&text[occurrence.range.clone()], occurrence.original_text);
                if let Some(key_range) = occurrence.key_range {
                    assert!(key_range.start >= occurrence.range.start);
                    assert!(key_range.end <= occurrence.range.end);
                    assert!(text.is_char_boundary(key_range.start));
                    assert!(text.is_char_boundary(key_range.end));
                }
                previous_end = occurrence.range.end;
            }
        }
    }
}
