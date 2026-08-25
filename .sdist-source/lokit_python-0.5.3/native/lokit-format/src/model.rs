//! Complete typed representation of Lokit's current Python dataclass graph.

use std::fmt;
use std::str::FromStr;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum TranslationStatus {
    New,
    Draft,
    Translated,
    Reviewed,
    Approved,
    Rejected,
    #[default]
    Unknown,
}

impl TranslationStatus {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::New => "new",
            Self::Draft => "draft",
            Self::Translated => "translated",
            Self::Reviewed => "reviewed",
            Self::Approved => "approved",
            Self::Rejected => "rejected",
            Self::Unknown => "unknown",
        }
    }
}

impl fmt::Display for TranslationStatus {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for TranslationStatus {
    type Err = ();

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "new" => Ok(Self::New),
            "draft" => Ok(Self::Draft),
            "translated" => Ok(Self::Translated),
            "reviewed" => Ok(Self::Reviewed),
            "approved" => Ok(Self::Approved),
            "rejected" => Ok(Self::Rejected),
            "unknown" => Ok(Self::Unknown),
            _ => Err(()),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PluralCategory {
    Generic,
    Zero,
    One,
    Two,
    Few,
    Many,
    Other,
}

impl PluralCategory {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Generic => "generic",
            Self::Zero => "zero",
            Self::One => "one",
            Self::Two => "two",
            Self::Few => "few",
            Self::Many => "many",
            Self::Other => "other",
        }
    }
}

impl fmt::Display for PluralCategory {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for PluralCategory {
    type Err = ();

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "generic" => Ok(Self::Generic),
            "zero" => Ok(Self::Zero),
            "one" => Ok(Self::One),
            "two" => Ok(Self::Two),
            "few" => Ok(Self::Few),
            "many" => Ok(Self::Many),
            "other" => Ok(Self::Other),
            _ => Err(()),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TieType {
    AOpen,
    AClose,
    AbbrOpen,
    AbbrClose,
    BOpen,
    BClose,
    BdiOpen,
    BdiClose,
    BdoOpen,
    BdoClose,
    Br,
    CiteOpen,
    CiteClose,
    CodeOpen,
    CodeClose,
    DataOpen,
    DataClose,
    DfnOpen,
    DfnClose,
    EmOpen,
    EmClose,
    IOpen,
    IClose,
    Img,
    KbdOpen,
    KbdClose,
    MarkOpen,
    MarkClose,
    QOpen,
    QClose,
    RpOpen,
    RpClose,
    RtOpen,
    RtClose,
    RubyOpen,
    RubyClose,
    SOpen,
    SClose,
    SampOpen,
    SampClose,
    SmallOpen,
    SmallClose,
    SpanOpen,
    SpanClose,
    StrongOpen,
    StrongClose,
    SubOpen,
    SubClose,
    SupOpen,
    SupClose,
    TimeOpen,
    TimeClose,
    UOpen,
    UClose,
    VarOpen,
    VarClose,
    Wbr,
    CustomOpen,
    CustomClose,
    CustomStandalone,
    PlaceholderStandalone,
}

impl TieType {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::AOpen => "a.open",
            Self::AClose => "a.close",
            Self::AbbrOpen => "abbr.open",
            Self::AbbrClose => "abbr.close",
            Self::BOpen => "b.open",
            Self::BClose => "b.close",
            Self::BdiOpen => "bdi.open",
            Self::BdiClose => "bdi.close",
            Self::BdoOpen => "bdo.open",
            Self::BdoClose => "bdo.close",
            Self::Br => "br.standalone",
            Self::CiteOpen => "cite.open",
            Self::CiteClose => "cite.close",
            Self::CodeOpen => "code.open",
            Self::CodeClose => "code.close",
            Self::DataOpen => "data.open",
            Self::DataClose => "data.close",
            Self::DfnOpen => "dfn.open",
            Self::DfnClose => "dfn.close",
            Self::EmOpen => "em.open",
            Self::EmClose => "em.close",
            Self::IOpen => "i.open",
            Self::IClose => "i.close",
            Self::Img => "img.standalone",
            Self::KbdOpen => "kbd.open",
            Self::KbdClose => "kbd.close",
            Self::MarkOpen => "mark.open",
            Self::MarkClose => "mark.close",
            Self::QOpen => "q.open",
            Self::QClose => "q.close",
            Self::RpOpen => "rp.open",
            Self::RpClose => "rp.close",
            Self::RtOpen => "rt.open",
            Self::RtClose => "rt.close",
            Self::RubyOpen => "ruby.open",
            Self::RubyClose => "ruby.close",
            Self::SOpen => "s.open",
            Self::SClose => "s.close",
            Self::SampOpen => "samp.open",
            Self::SampClose => "samp.close",
            Self::SmallOpen => "small.open",
            Self::SmallClose => "small.close",
            Self::SpanOpen => "span.open",
            Self::SpanClose => "span.close",
            Self::StrongOpen => "strong.open",
            Self::StrongClose => "strong.close",
            Self::SubOpen => "sub.open",
            Self::SubClose => "sub.close",
            Self::SupOpen => "sup.open",
            Self::SupClose => "sup.close",
            Self::TimeOpen => "time.open",
            Self::TimeClose => "time.close",
            Self::UOpen => "u.open",
            Self::UClose => "u.close",
            Self::VarOpen => "var.open",
            Self::VarClose => "var.close",
            Self::Wbr => "wbr.standalone",
            Self::CustomOpen => "custom.open",
            Self::CustomClose => "custom.close",
            Self::CustomStandalone => "custom.standalone",
            Self::PlaceholderStandalone => "placeholder.standalone",
        }
    }

    pub fn is_open(self) -> bool {
        self.as_str().ends_with(".open")
    }

    pub fn is_close(self) -> bool {
        self.as_str().ends_with(".close")
    }
}

impl fmt::Display for TieType {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for TieType {
    type Err = ();

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let parsed = match value {
            "a.open" => Self::AOpen,
            "a.close" => Self::AClose,
            "abbr.open" => Self::AbbrOpen,
            "abbr.close" => Self::AbbrClose,
            "b.open" => Self::BOpen,
            "b.close" => Self::BClose,
            "bdi.open" => Self::BdiOpen,
            "bdi.close" => Self::BdiClose,
            "bdo.open" => Self::BdoOpen,
            "bdo.close" => Self::BdoClose,
            "br.standalone" => Self::Br,
            "cite.open" => Self::CiteOpen,
            "cite.close" => Self::CiteClose,
            "code.open" => Self::CodeOpen,
            "code.close" => Self::CodeClose,
            "data.open" => Self::DataOpen,
            "data.close" => Self::DataClose,
            "dfn.open" => Self::DfnOpen,
            "dfn.close" => Self::DfnClose,
            "em.open" => Self::EmOpen,
            "em.close" => Self::EmClose,
            "i.open" => Self::IOpen,
            "i.close" => Self::IClose,
            "img.standalone" => Self::Img,
            "kbd.open" => Self::KbdOpen,
            "kbd.close" => Self::KbdClose,
            "mark.open" => Self::MarkOpen,
            "mark.close" => Self::MarkClose,
            "q.open" => Self::QOpen,
            "q.close" => Self::QClose,
            "rp.open" => Self::RpOpen,
            "rp.close" => Self::RpClose,
            "rt.open" => Self::RtOpen,
            "rt.close" => Self::RtClose,
            "ruby.open" => Self::RubyOpen,
            "ruby.close" => Self::RubyClose,
            "s.open" => Self::SOpen,
            "s.close" => Self::SClose,
            "samp.open" => Self::SampOpen,
            "samp.close" => Self::SampClose,
            "small.open" => Self::SmallOpen,
            "small.close" => Self::SmallClose,
            "span.open" => Self::SpanOpen,
            "span.close" => Self::SpanClose,
            "strong.open" => Self::StrongOpen,
            "strong.close" => Self::StrongClose,
            "sub.open" => Self::SubOpen,
            "sub.close" => Self::SubClose,
            "sup.open" => Self::SupOpen,
            "sup.close" => Self::SupClose,
            "time.open" => Self::TimeOpen,
            "time.close" => Self::TimeClose,
            "u.open" => Self::UOpen,
            "u.close" => Self::UClose,
            "var.open" => Self::VarOpen,
            "var.close" => Self::VarClose,
            "wbr.standalone" => Self::Wbr,
            "custom.open" => Self::CustomOpen,
            "custom.close" => Self::CustomClose,
            "custom.standalone" => Self::CustomStandalone,
            "placeholder.standalone" => Self::PlaceholderStandalone,
            _ => return Err(()),
        };
        Ok(parsed)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Plural {
    pub variant: String,
    pub count: Option<i64>,
    pub category: Option<PluralCategory>,
    pub extensions: Vec<(String, String)>,
}

impl Plural {
    pub fn new(variant: impl Into<String>) -> Self {
        Self {
            variant: variant.into(),
            count: None,
            category: None,
            extensions: Vec::new(),
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Meta {
    pub usage_count: Option<i64>,
    pub last_used: Option<String>,
    pub first_used: Option<String>,
    pub created: Option<String>,
    pub updated: Option<String>,
    pub max_length: Option<i64>,
    pub min_length: Option<i64>,
    pub extensions: Vec<(String, String)>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Origin {
    pub system: Option<String>,
    pub project: Option<String>,
    pub creator_id: Option<String>,
    pub extensions: Vec<(String, String)>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Comment {
    pub context: String,
    pub timestamp: Option<String>,
    pub origin: Option<Origin>,
    pub context_key: Option<String>,
    pub extensions: Vec<(String, String)>,
}

impl Comment {
    pub fn new(context: impl Into<String>) -> Self {
        Self {
            context: context.into(),
            timestamp: None,
            origin: None,
            context_key: None,
            extensions: Vec::new(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TextPart {
    pub value: String,
}

impl TextPart {
    pub fn new(value: impl Into<String>) -> Self {
        Self {
            value: value.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CodePart {
    pub r#ref: String,
}

impl CodePart {
    pub fn new(reference: impl Into<String>) -> Self {
        Self {
            r#ref: reference.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SegmentPart {
    Text(TextPart),
    Code(CodePart),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TieData {
    pub id: String,
    pub r#type: TieType,
    pub attributes: Vec<(String, String)>,
    pub attribute_data: String,
    pub position: i64,
    pub order: i64,
    pub pair_id: Option<String>,
    pub original_name: Option<String>,
    pub original_text: Option<String>,
}

impl TieData {
    pub fn new(id: impl Into<String>, tie_type: TieType) -> Self {
        Self {
            id: id.into(),
            r#type: tie_type,
            attributes: Vec::new(),
            attribute_data: String::new(),
            position: 0,
            order: 0,
            pair_id: None,
            original_name: None,
            original_text: None,
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Tags {
    pub source_tag_map: Vec<(String, TieData)>,
    pub target_tag_map: Vec<(String, TieData)>,
    pub source_parts: Vec<SegmentPart>,
    pub target_parts: Vec<SegmentPart>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct TargetTags {
    pub tag_map: Vec<(String, TieData)>,
    pub parts: Vec<SegmentPart>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct TargetData {
    pub text: Option<String>,
    pub status: TranslationStatus,
    pub tags: Option<TargetTags>,
    pub plural: Option<Plural>,
    pub meta: Meta,
    pub comments: Vec<Comment>,
    pub extensions: Vec<(String, String)>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct AdjacentContext {
    pub unit_id: Option<String>,
    pub source: Option<String>,
    pub target: Option<String>,
    pub extensions: Vec<(String, String)>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Data {
    pub source: String,
    pub target: Option<String>,
    pub targets: Vec<(String, TargetData)>,
    pub plural: Option<Plural>,
    pub tags: Option<Tags>,
    pub meta: Meta,
    pub status: TranslationStatus,
    pub comments: Vec<Comment>,
    pub previous_context: Option<AdjacentContext>,
    pub next_context: Option<AdjacentContext>,
    pub extensions: Vec<(String, String)>,
}

impl Data {
    pub fn new(source: impl Into<String>) -> Self {
        Self {
            source: source.into(),
            target: None,
            targets: Vec::new(),
            plural: None,
            tags: None,
            meta: Meta::default(),
            status: TranslationStatus::Unknown,
            comments: Vec::new(),
            previous_context: None,
            next_context: None,
            extensions: Vec::new(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BaseStructure {
    pub source_locale: String,
    pub target_locale: Option<String>,
    pub data: Vec<(String, Data)>,
    pub target_locales: Vec<String>,
    pub format_version: String,
    pub export_origin: String,
    pub export_timestamp: String,
    pub source_language: Option<String>,
    pub target_language: Option<String>,
    pub target_languages: Vec<String>,
    pub extensions: Vec<(String, String)>,
}

impl BaseStructure {
    pub fn new(source_locale: impl Into<String>) -> Self {
        Self {
            source_locale: source_locale.into(),
            target_locale: None,
            data: Vec::new(),
            target_locales: Vec::new(),
            format_version: "0.1".to_owned(),
            export_origin: String::new(),
            export_timestamp: String::new(),
            source_language: None,
            target_language: None,
            target_languages: Vec::new(),
            extensions: Vec::new(),
        }
    }
}
