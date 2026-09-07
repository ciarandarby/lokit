use std::collections::HashMap;
use std::str::FromStr;

use lokit_format::{
    AdjacentContext, CodePart, Comment, Data, Meta, Origin, SegmentPart, Tags, TargetData,
    TargetTags, TextPart, TieData, TieType, TranslationStatus,
};
use quick_xml::encoding::Decoder;
use quick_xml::events::BytesStart;
use quick_xml::XmlVersion;

use crate::{
    canonical_locale, normalize_extension_key, same_locale, InterchangeFormat, NativeError,
    NativeResult, ParseMode,
};

const MAX_CONTEXT_BYTES: usize = 64 * 1024 * 1024;
const MAX_DEPTH: usize = 256;

#[derive(Debug)]
enum Content {
    Text(String),
    Element(Node),
}

#[derive(Debug)]
pub(crate) struct Node {
    name: String,
    attrs: Vec<(String, String)>,
    content: Vec<Content>,
    bytes: usize,
}

impl Node {
    pub(crate) fn is_ignorable(&self) -> bool {
        self.local() == "ignorable"
    }
    fn local(&self) -> &str {
        self.name.rsplit(':').next().unwrap_or(&self.name)
    }

    fn attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    fn children(&self) -> impl Iterator<Item = &Node> {
        self.content.iter().filter_map(|part| match part {
            Content::Element(node) => Some(node),
            _ => None,
        })
    }

    fn lokit_status(&self, namespaces: &[(String, String)]) -> Option<&str> {
        self.attrs.iter().find_map(|(key, value)| {
            let (prefix, local) = key.split_once(':')?;
            (local == "status"
                && namespaces
                    .iter()
                    .chain(self.attrs.iter())
                    .any(|(key, value)| {
                        key == &format!("xmlns:{prefix}") && value == "urn:lokit:provenance:1"
                    }))
            .then_some(value.as_str())
        })
    }

    fn child(&self, name: &str) -> Option<&Node> {
        self.children().find(|node| node.local() == name)
    }

    fn text(&self) -> String {
        let mut result = String::new();
        self.append_text(&mut result);
        result
    }

    fn append_text(&self, result: &mut String) {
        for part in &self.content {
            match part {
                Content::Text(text) => result.push_str(text),
                Content::Element(node) => node.append_text(result),
            }
        }
    }

    fn initial_text(&self) -> Option<String> {
        match self.content.first() {
            Some(Content::Text(text)) => Some(text.clone()),
            _ => None,
        }
    }

    fn xml(&self, output: &mut String) {
        output.push('<');
        output.push_str(&self.name);
        for (key, value) in &self.attrs {
            output.push(' ');
            output.push_str(key);
            output.push_str("=\"");
            output.push_str(&quick_xml::escape::escape(value));
            output.push('"');
        }
        if self.content.is_empty() {
            output.push_str("/>");
            return;
        }
        output.push('>');
        for part in &self.content {
            match part {
                Content::Text(text) => output.push_str(&quick_xml::escape::escape(text)),
                Content::Element(node) => node.xml(output),
            }
        }
        output.push_str("</");
        output.push_str(&self.name);
        output.push('>');
    }

    fn xml_len(&self) -> usize {
        let attrs = self.attrs.iter().fold(0usize, |bytes, (key, value)| {
            bytes
                .saturating_add(key.len())
                .saturating_add(escaped_len(value))
                .saturating_add(4)
        });
        let mut bytes = self.name.len().saturating_add(attrs).saturating_add(3);
        if !self.content.is_empty() {
            bytes = bytes.saturating_add(self.name.len()).saturating_add(2);
            for part in &self.content {
                bytes = bytes.saturating_add(match part {
                    Content::Text(value) => escaped_len(value),
                    Content::Element(node) => node.xml_len(),
                });
            }
        }
        bytes
    }

    fn serialized_xml(&self, budget: &mut usize) -> NativeResult<String> {
        let bytes = self.xml_len();
        charge_budget(budget, bytes)?;
        let mut output = String::with_capacity(bytes);
        self.xml(&mut output);
        Ok(output)
    }
}

fn escaped_len(value: &str) -> usize {
    value.bytes().fold(0usize, |size, byte| {
        size.saturating_add(match byte {
            b'&' => 5,
            b'<' | b'>' => 4,
            b'\'' | b'"' => 6,
            _ => 1,
        })
    })
}

fn charge_budget(remaining: &mut usize, bytes: usize) -> NativeResult<()> {
    *remaining = remaining
        .checked_sub(bytes)
        .ok_or_else(|| NativeError::Invalid("XML expanded semantic data exceeds 64 MiB".into()))?;
    Ok(())
}

#[derive(Default)]
pub(crate) struct Capture {
    stack: Vec<Node>,
    bytes: usize,
}

impl Capture {
    pub(crate) fn xliff_header(&self, extensions: &mut HashMap<String, String>) {
        fn visit(node: &Node, extensions: &mut HashMap<String, String>) {
            if node.local() == "prop" {
                let key = match node.attr("prop-type").unwrap_or("") {
                    "x-po-metadata-json" => Some("po_metadata_json"),
                    "x-po-header-translator-comments" => Some("po_header_translator_comments"),
                    "x-po-header-extracted-comments" => Some("po_header_extracted_comments"),
                    "x-po-header-flags" => Some("po_header_flags"),
                    "x-po-header-previous" => Some("po_header_previous"),
                    _ => None,
                };
                if let Some(key) = key {
                    extensions.insert(key.into(), node.text());
                }
            }
            for child in node.children() {
                visit(child, extensions);
            }
        }
        if let Some(node) = self.stack.last() {
            visit(node, extensions);
        }
    }

    pub(crate) fn has_unit_context(&self) -> bool {
        self.stack.iter().any(|node| {
            matches!(node.local(), "file" | "unit" | "group")
                && (node.attrs.iter().any(|(key, _)| {
                    !matches!(
                        key.as_str(),
                        "id" | "xmlns"
                            | "original"
                            | "datatype"
                            | "source-language"
                            | "target-language"
                    ) && !key.starts_with("xmlns:")
                }) || !node.content.is_empty())
        })
    }

    pub(crate) fn start(&mut self, element: &BytesStart<'_>, decoder: Decoder) -> NativeResult<()> {
        if self.stack.len() >= MAX_DEPTH {
            return Err(NativeError::Invalid(
                "XML nesting exceeds 256 elements".into(),
            ));
        }
        let name = decoder
            .decode(element.name().as_ref())
            .map_err(|error| NativeError::Invalid(error.to_string()))?
            .into_owned();
        let mut attrs = Vec::new();
        let mut bytes = std::mem::size_of::<Node>() + name.len();
        for attribute in element.attributes() {
            let attribute = attribute.map_err(|error| NativeError::Xml(error.into()))?;
            let key = decoder
                .decode(attribute.key.as_ref())
                .map_err(|error| NativeError::Invalid(error.to_string()))?
                .into_owned();
            let value = attribute
                .decoded_and_normalized_value(XmlVersion::Explicit1_0, decoder)?
                .into_owned();
            crate::validate_xml_1_0_chars(&value, "XML attribute")?;
            bytes = bytes.saturating_add(key.len() + value.len() + 48);
            attrs.push((key, value));
        }
        self.charge(bytes)?;
        self.stack.push(Node {
            name,
            attrs,
            content: Vec::new(),
            bytes,
        });
        Ok(())
    }

    fn charge(&mut self, bytes: usize) -> NativeResult<()> {
        self.bytes = self.bytes.saturating_add(bytes);
        if self.bytes > MAX_CONTEXT_BYTES {
            return Err(NativeError::Invalid(
                "XML retained context exceeds 64 MiB".into(),
            ));
        }
        Ok(())
    }

    pub(crate) fn text(&mut self, value: &str) -> NativeResult<()> {
        if self.stack.is_empty() {
            return Ok(());
        }
        if self.stack.last().is_some_and(|node| {
            matches!(
                node.local(),
                "tmx" | "xliff" | "body" | "file" | "group" | "unit"
            )
        }) && value.trim().is_empty()
        {
            return Ok(());
        }
        let extra = value.len().saturating_add(std::mem::size_of::<Content>());
        self.charge(extra)?;
        let node = self.stack.last_mut().expect("checked stack");
        node.bytes = node.bytes.saturating_add(extra);
        if let Some(Content::Text(text)) = node.content.last_mut() {
            text.push_str(value);
        } else {
            node.content.push(Content::Text(value.to_owned()));
        }
        Ok(())
    }

    pub(crate) fn end(&mut self, record: bool) -> Option<Node> {
        let node = self.stack.pop()?;
        if record {
            self.bytes = self.bytes.saturating_sub(node.bytes);
            return Some(node);
        }
        let scope = matches!(
            node.local(),
            "tmx" | "xliff" | "body" | "file" | "group" | "unit"
        );
        if !scope {
            if let Some(parent) = self.stack.last_mut() {
                parent.bytes = parent.bytes.saturating_add(node.bytes);
                parent.content.push(Content::Element(node));
                return None;
            }
        }
        self.bytes = self.bytes.saturating_sub(node.bytes);
        None
    }

    pub(crate) fn fragment(&self, node: &Node) -> NativeResult<Vec<u8>> {
        let namespaces = self.namespaces();
        let bytes = namespaces.iter().fold(
            "<lokit-fragment>".len() + "</lokit-fragment>".len() + node.xml_len(),
            |bytes, (prefix, uri)| {
                bytes
                    .saturating_add(prefix.len())
                    .saturating_add(escaped_len(uri))
                    .saturating_add(4)
            },
        );
        let mut budget = MAX_CONTEXT_BYTES;
        charge_budget(&mut budget, bytes)?;
        let mut value = String::with_capacity(bytes);
        value.push_str("<lokit-fragment");
        for (prefix, uri) in namespaces {
            value.push(' ');
            value.push_str(&prefix);
            value.push_str("=\"");
            value.push_str(&quick_xml::escape::escape(&uri));
            value.push('"');
        }
        value.push('>');
        node.xml(&mut value);
        value.push_str("</lokit-fragment>");
        Ok(value.into_bytes())
    }

    fn namespaces(&self) -> Vec<(String, String)> {
        let mut result = Vec::new();
        for node in &self.stack {
            merge_namespaces(&mut result, node);
        }
        result
    }

    pub(crate) fn data(
        &self,
        node: &Node,
        format: InterchangeFormat,
        source: &str,
        target: Option<&str>,
        mode: ParseMode,
    ) -> NativeResult<Data> {
        let mut namespaces = self.namespaces();
        merge_namespaces(&mut namespaces, node);
        match format {
            InterchangeFormat::Tmx => tmx(node, source, target, mode, &namespaces),
            InterchangeFormat::Xliff => self.xliff(node, target, &namespaces),
        }
    }

    fn xliff(
        &self,
        node: &Node,
        target_locale: Option<&str>,
        namespaces: &[(String, String)],
    ) -> NativeResult<Data> {
        let mut original_data = HashMap::new();
        let mut data = Data::new("");
        let mut inline_budget = MAX_CONTEXT_BYTES;
        let mut group_index = 0;
        for ancestor in &self.stack {
            if matches!(ancestor.local(), "file" | "group" | "unit") {
                if ancestor.local() == "group" {
                    retain_attrs(
                        ancestor,
                        &format!("xliff.group.{group_index}"),
                        &mut data.extensions,
                    );
                    group_index += 1;
                }
                retain_attrs(
                    ancestor,
                    &format!("xliff.{}", ancestor.local()),
                    &mut data.extensions,
                );
                for child in ancestor.children() {
                    if child.local() == "originalData" {
                        for payload in child.children().filter(|entry| entry.local() == "data") {
                            if let Some(id) = payload.attr("id") {
                                original_data.insert(id.to_owned(), payload.text());
                                data.extensions
                                    .push((format!("xliff.originalData.{id}"), payload.text()));
                            }
                        }
                    } else {
                        xliff_metadata(child, &mut data, &mut inline_budget)?;
                    }
                }
            }
        }
        retain_attrs(
            node,
            &format!("xliff.{}", node.local()),
            &mut data.extensions,
        );
        if let Some(restype) = node.attr("restype") {
            data.extensions
                .push(("xliff_restype".into(), restype.into()));
        }
        for child in node
            .children()
            .filter(|child| !matches!(child.local(), "source" | "target"))
        {
            xliff_metadata(child, &mut data, &mut inline_budget)?;
        }
        let source = inline(
            node.child("source"),
            false,
            namespaces,
            &original_data,
            &mut inline_budget,
        )?;
        let target = inline(
            node.child("target"),
            false,
            namespaces,
            &original_data,
            &mut inline_budget,
        )?;
        for name in ["source", "target"] {
            if let Some(child) = node.child(name) {
                retain_attrs(child, &format!("xliff.{name}"), &mut data.extensions);
            }
        }
        data.source = source.text.clone();
        let status = if matches!(node.local(), "segment" | "ignorable") {
            crate::xliff_v2_status(node.attr("state").unwrap_or("initial"))
        } else if let Some(target) = node.child("target") {
            crate::xliff_status(target.attr("state").unwrap_or(""))
        } else {
            "unknown"
        };
        data.status = TranslationStatus::from_str(status).unwrap_or_default();
        if node.local() == "ignorable" {
            data.extensions
                .push(("xliff.ignorable".into(), "true".into()));
        }
        if let Some(state) = node
            .child("target")
            .and_then(|target| target.lokit_status(namespaces))
            .or_else(|| node.lokit_status(namespaces))
        {
            data.status = TranslationStatus::from_str(state).unwrap_or(data.status);
        }
        if let Some(_target_node) = node.child("target") {
            if let Some(locale) = target_locale {
                data.targets.push((
                    canonical_locale(locale),
                    TargetData {
                        text: Some(target.text.clone()),
                        status: data.status,
                        tags: target.tags(),
                        ..TargetData::default()
                    },
                ));
            } else {
                data.target = Some(target.text.clone());
            }
        }
        data.tags = combine(source.tags, source.parts, target.tags, target.parts);
        gettext(&mut data);
        Ok(data)
    }
}

fn merge_namespaces(result: &mut Vec<(String, String)>, node: &Node) {
    for (key, value) in &node.attrs {
        if key == "xmlns" || key.starts_with("xmlns:") {
            set(result, key.clone(), value.clone());
        }
    }
}

fn set(values: &mut Vec<(String, String)>, key: String, value: String) {
    if let Some((_, old)) = values.iter_mut().find(|(candidate, _)| candidate == &key) {
        *old = value;
    } else {
        values.push((key, value));
    }
}

fn retain_attrs(node: &Node, prefix: &str, target: &mut Vec<(String, String)>) {
    for (key, value) in &node.attrs {
        if key != "xmlns" && !key.starts_with("xmlns:") {
            set(target, format!("{prefix}.{key}"), value.clone());
        }
    }
}

#[derive(Default)]
struct Inline {
    text: String,
    tags: Vec<(String, TieData)>,
    parts: Vec<SegmentPart>,
    position: i64,
    pairs: HashMap<String, String>,
}

impl Inline {
    fn tags(&self) -> Option<TargetTags> {
        (!self.tags.is_empty()).then(|| TargetTags {
            tag_map: self.tags.clone(),
            parts: self.parts.clone(),
        })
    }

    fn code(
        &mut self,
        node: &Node,
        kind: TieType,
        pair: Option<String>,
        original: Option<String>,
        namespaces: &[(String, String)],
    ) {
        let id = format!("c{}", self.tags.len());
        let mut tag = TieData::new(&id, kind);
        tag.position = self.position;
        tag.order = self.tags.len() as i64;
        tag.pair_id = pair;
        tag.original_name = Some(node.name.clone());
        tag.original_text = original;
        tag.attributes = node
            .attrs
            .iter()
            .filter(|(key, _)| key != "xmlns" && !key.starts_with("xmlns:"))
            .cloned()
            .collect();
        let mut required = Vec::new();
        for name in std::iter::once(node.name.as_str())
            .chain(tag.attributes.iter().map(|(key, _)| key.as_str()))
        {
            if let Some((prefix, _)) = name.split_once(':') {
                if prefix != "xml" && !required.contains(&prefix) {
                    required.push(prefix);
                }
            }
        }
        if !required.is_empty() {
            tag.attribute_data = "lokit:xml-namespaces\n".into();
            let entries: Vec<String> = namespaces
                .iter()
                .filter_map(|(key, value)| {
                    key.strip_prefix("xmlns:")
                        .filter(|prefix| required.contains(prefix))
                        .map(|prefix| format!("{prefix}={value}"))
                })
                .collect();
            tag.attribute_data.push_str(&entries.join("\n"));
        }
        self.parts.push(SegmentPart::Code(CodePart::new(&id)));
        self.tags.push((id, tag));
    }

    fn pair(&mut self, node: &Node, tmx: bool, container: bool) -> Option<String> {
        let key = if tmx {
            node.attr("i").or_else(|| node.attr("id"))
        } else {
            node.attr("rid")
                .or_else(|| node.attr("startRef"))
                .or_else(|| node.attr("id"))
                .or_else(|| node.attr("xid"))
                .or_else(|| node.attr("ctype"))
        };
        let generated;
        let key = if let Some(key) = key {
            key
        } else if container {
            generated = format!("__generated_{}", self.pairs.len());
            &generated
        } else {
            return None;
        };
        let next = format!("p{}", self.pairs.len());
        Some(self.pairs.entry(key.to_owned()).or_insert(next).clone())
    }

    fn content(
        &mut self,
        node: &Node,
        tmx: bool,
        namespaces: &[(String, String)],
        original_data: &HashMap<String, String>,
    ) {
        for part in &node.content {
            match part {
                Content::Text(value) => {
                    self.position += value.chars().count() as i64;
                    self.text.push_str(value);
                    self.parts.push(SegmentPart::Text(TextPart::new(value)));
                }
                Content::Element(child) => {
                    let mut child_namespaces = namespaces.to_vec();
                    merge_namespaces(&mut child_namespaces, child);
                    let name = child.local();
                    let container = if tmx {
                        matches!(name, "hi" | "sub")
                    } else {
                        matches!(name, "g" | "mrk" | "pc" | "sub")
                            || (!matches!(
                                name,
                                "bpt"
                                    | "bx"
                                    | "cp"
                                    | "ec"
                                    | "em"
                                    | "ept"
                                    | "ex"
                                    | "it"
                                    | "ph"
                                    | "sc"
                                    | "sm"
                                    | "ut"
                                    | "x"
                            ) && !child.content.is_empty())
                    };
                    let pair = self.pair(child, tmx, container);
                    if container {
                        let (open, close) = semantic_types(child, tmx);
                        let opening = child
                            .attr("dataRefStart")
                            .and_then(|id| original_data.get(id))
                            .cloned();
                        let closing = child
                            .attr("dataRefEnd")
                            .and_then(|id| original_data.get(id))
                            .cloned();
                        self.code(child, open, pair.clone(), opening, &child_namespaces);
                        self.content(child, tmx, &child_namespaces, original_data);
                        self.code(child, close, pair, closing, &child_namespaces);
                    } else {
                        let kind = match name {
                            "bpt" | "bx" | "sc" => TieType::CustomOpen,
                            "ept" | "ex" | "ec" => TieType::CustomClose,
                            "it" if child.attr("pos") == Some("begin") => TieType::CustomOpen,
                            "it" if child.attr("pos") == Some("end") => TieType::CustomClose,
                            _ => TieType::CustomStandalone,
                        };
                        let original = child
                            .attr("dataRef")
                            .and_then(|id| original_data.get(id))
                            .cloned()
                            .or_else(|| child.initial_text());
                        self.code(child, kind, pair, original, &child_namespaces);
                    }
                }
            }
        }
    }
}

fn semantic_types(node: &Node, tmx: bool) -> (TieType, TieType) {
    if tmx {
        return (TieType::CustomOpen, TieType::CustomClose);
    }
    match node
        .attr("ctype")
        .or_else(|| node.attr("type"))
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "bold" | "b" | "x-bold" | "fmt:bold" => (TieType::BOpen, TieType::BClose),
        "italic" | "i" | "x-italic" | "fmt:italic" => (TieType::IOpen, TieType::IClose),
        "emphasis" | "em" | "fmt:emphasis" => (TieType::EmOpen, TieType::EmClose),
        "strong" | "fmt:strong" => (TieType::StrongOpen, TieType::StrongClose),
        _ => (TieType::CustomOpen, TieType::CustomClose),
    }
}

fn inline(
    node: Option<&Node>,
    tmx: bool,
    namespaces: &[(String, String)],
    original_data: &HashMap<String, String>,
    budget: &mut usize,
) -> NativeResult<Inline> {
    let mut result = Inline::default();
    if let Some(node) = node {
        let namespace_bytes = namespaces.iter().fold(0usize, |bytes, (key, value)| {
            bytes
                .saturating_add(key.len())
                .saturating_add(value.len())
                .saturating_add(48)
        });
        validate_inline_budget(node, original_data, namespace_bytes, budget)?;
        let mut namespaces = namespaces.to_vec();
        merge_namespaces(&mut namespaces, node);
        result.content(node, tmx, &namespaces, original_data);
        if result.tags.is_empty() {
            result.parts.clear();
        }
    }
    Ok(result)
}

fn validate_inline_budget(
    node: &Node,
    original_data: &HashMap<String, String>,
    namespace_bytes: usize,
    budget: &mut usize,
) -> NativeResult<()> {
    let namespace_bytes = node
        .attrs
        .iter()
        .filter(|(key, _)| key == "xmlns" || key.starts_with("xmlns:"))
        .fold(namespace_bytes, |bytes, (key, value)| {
            bytes
                .saturating_add(key.len())
                .saturating_add(value.len())
                .saturating_add(48)
        });
    charge_budget(budget, namespace_bytes.saturating_mul(2))?;
    let attrs = node.attrs.iter().fold(0usize, |bytes, (key, value)| {
        bytes
            .saturating_add(key.len())
            .saturating_add(value.len())
            .saturating_add(48)
    });
    charge_budget(
        budget,
        attrs
            .saturating_add(node.name.len())
            .saturating_add(std::mem::size_of::<TieData>() + 256)
            .saturating_mul(2),
    )?;
    for name in ["dataRef", "dataRefStart", "dataRefEnd"] {
        if let Some(payload) = node.attr(name).and_then(|id| original_data.get(id)) {
            charge_budget(budget, payload.len().saturating_mul(2))?;
        }
    }
    for part in &node.content {
        match part {
            Content::Text(value) => {
                charge_budget(budget, value.len().saturating_mul(2).saturating_add(64))?
            }
            Content::Element(child) => {
                validate_inline_budget(child, original_data, namespace_bytes, budget)?
            }
        }
    }
    Ok(())
}

fn combine(
    source_tag_map: Vec<(String, TieData)>,
    source_parts: Vec<SegmentPart>,
    target_tag_map: Vec<(String, TieData)>,
    target_parts: Vec<SegmentPart>,
) -> Option<Tags> {
    if source_tag_map.is_empty() && target_tag_map.is_empty() {
        None
    } else {
        Some(Tags {
            source_tag_map,
            source_parts,
            target_tag_map,
            target_parts,
        })
    }
}

fn tmx(
    node: &Node,
    source_locale: &str,
    requested_target: Option<&str>,
    mode: ParseMode,
    namespaces: &[(String, String)],
) -> NativeResult<Data> {
    let mut data = Data::new("");
    let mut inline_budget = MAX_CONTEXT_BYTES;
    tmx_metadata(node, &mut data, mode);
    let mut source = Inline::default();
    let mut target = Inline::default();
    for variant in node.children().filter(|child| child.local() == "tuv") {
        let locale = canonical_locale(
            variant
                .attr("xml:lang")
                .or_else(|| variant.attr("lang"))
                .unwrap_or(""),
        );
        let is_source = same_locale(&locale, source_locale);
        if !is_source && requested_target.is_some_and(|target| !same_locale(target, &locale)) {
            continue;
        }
        let mut variant_namespaces = namespaces.to_vec();
        merge_namespaces(&mut variant_namespaces, node);
        merge_namespaces(&mut variant_namespaces, variant);
        let content = inline(
            variant.child("seg"),
            true,
            &variant_namespaces,
            &HashMap::new(),
            &mut inline_budget,
        )?;
        let mut fields = Data::new("");
        tmx_metadata(variant, &mut fields, mode);
        if is_source {
            source = content;
            if mode == ParseMode::Full {
                for (key, value) in &variant.attrs {
                    if key != "xml:lang" && key != "lang" {
                        data.extensions
                            .push((format!("tmx.source.{key}"), value.clone()));
                    }
                }
                for (index, child) in variant
                    .children()
                    .filter(|child| matches!(child.local(), "prop" | "note"))
                    .enumerate()
                {
                    let xml = child.serialized_xml(&mut inline_budget)?;
                    data.extensions
                        .push((format!("tmx.source.metadata.{index}"), xml));
                }
            }
        } else if !locale.is_empty() {
            let target_data = TargetData {
                text: (!content.text.is_empty()).then(|| content.text.clone()),
                status: if fields.status == TranslationStatus::Unknown {
                    data.status
                } else {
                    fields.status
                },
                tags: content.tags(),
                meta: fields.meta,
                comments: fields.comments,
                extensions: fields.extensions,
                ..TargetData::default()
            };
            if requested_target.is_some() {
                data.target = target_data.text.clone();
                target = content;
                if target_data.meta != Meta::default()
                    || !target_data.comments.is_empty()
                    || !target_data.extensions.is_empty()
                    || target_data.status != data.status
                {
                    data.targets.push((locale, target_data));
                }
            } else {
                data.targets.push((locale, target_data));
            }
        }
    }
    data.source = source.text;
    data.tags = combine(source.tags, source.parts, target.tags, target.parts);
    gettext(&mut data);
    Ok(data)
}

fn tmx_metadata(node: &Node, data: &mut Data, mode: ParseMode) {
    if mode == ParseMode::Full {
        data.meta = Meta {
            usage_count: node
                .attr("usagecount")
                .and_then(|value| value.parse::<i64>().ok())
                .filter(|value| *value >= 0),
            created: node
                .attr("creationdate")
                .filter(|value| !value.is_empty())
                .map(str::to_owned),
            updated: node.attr("changedate").map(str::to_owned),
            last_used: node.attr("lastusagedate").map(str::to_owned),
            ..Meta::default()
        };
        for (key, value) in &node.attrs {
            if !matches!(
                key.as_str(),
                "tuid"
                    | "xml:lang"
                    | "lang"
                    | "usagecount"
                    | "creationdate"
                    | "changedate"
                    | "lastusagedate"
            ) && key != "xmlns"
                && !key.starts_with("xmlns:")
            {
                data.meta
                    .extensions
                    .push((normalize_extension_key(key), value.clone()));
            }
        }
    }
    let mut origin = Origin {
        creator_id: node.attr("creationid").map(str::to_owned),
        ..Origin::default()
    };
    let mut context = None;
    let mut previous = AdjacentContext::default();
    let mut next = AdjacentContext::default();
    for child in node.children() {
        let key = child.attr("type").unwrap_or("").to_ascii_lowercase();
        if child.local() == "prop" && crate::is_status_property(&key) && mode.includes_status() {
            let status = crate::tmx_status(child.text().trim());
            if status_rank(status) > status_rank(data.status.as_str()) {
                data.status = TranslationStatus::from_str(status).unwrap_or_default();
            }
        }
        if mode != ParseMode::Full {
            continue;
        }
        if child.local() == "note" {
            let mut comment = Comment::new(child.text());
            comment.timestamp = child
                .attr("changedate")
                .or_else(|| node.attr("changedate"))
                .map(str::to_owned);
            retain_attrs(child, "tmx.note", &mut comment.extensions);
            data.comments.push(comment);
        } else if child.local() == "prop" {
            let value = child.text();
            if let Some(key) = gettext_key(&key) {
                append_gettext(&mut data.extensions, key, &value);
            }
            match key.as_str() {
                "x-project" => origin.project = Some(value),
                "x-system" => origin.system = Some(value),
                "x-domain" => set(&mut data.extensions, "domain".into(), value),
                "x-context" | "x-key" => context = Some(value),
                "note" | "x-note" | "comment" | "x-comment" => {
                    data.comments.push(Comment::new(value))
                }
                "x-previous-id" => previous.unit_id = Some(value),
                "x-previous-source" | "x-previous-source-text" => previous.source = Some(value),
                "x-previous-target" | "x-previous-target-text" => previous.target = Some(value),
                "x-next-id" => next.unit_id = Some(value),
                "x-next-source" | "x-next-source-text" => next.source = Some(value),
                "x-next-target" | "x-next-target-text" => next.target = Some(value),
                "x-po-translator-comment" | "x-po-extracted-comment" => {
                    let mut comment = Comment::new(value);
                    comment.extensions.push((
                        "po_comment_kind".into(),
                        if key == "x-po-translator-comment" {
                            "translator"
                        } else {
                            "extracted"
                        }
                        .into(),
                    ));
                    data.comments.push(comment);
                }
                _ if !crate::is_status_property(&key) => set(
                    &mut data.extensions,
                    format!("property.{}", normalize_extension_key(&key)),
                    value,
                ),
                _ => {}
            }
        }
    }
    if origin != Origin::default() || context.is_some() {
        if data.comments.is_empty() {
            data.comments.push(Comment::new(""));
        }
        for comment in &mut data.comments {
            if origin != Origin::default() {
                comment.origin = Some(origin.clone());
            }
            comment.context_key = context.clone();
            if comment.timestamp.is_none() {
                comment.timestamp = node.attr("changedate").map(str::to_owned);
            }
        }
    }
    if previous != AdjacentContext::default() {
        data.previous_context = Some(previous);
    }
    if next != AdjacentContext::default() {
        data.next_context = Some(next);
    }
    let structured: std::collections::HashSet<String> = data
        .comments
        .iter()
        .filter(|comment| {
            comment
                .extensions
                .iter()
                .any(|(key, _)| key == "po_comment_kind")
        })
        .map(|comment| comment.context.clone())
        .collect();
    data.comments.retain(|comment| {
        !structured.contains(&comment.context)
            || comment
                .extensions
                .iter()
                .any(|(key, _)| key == "po_comment_kind")
    });
}

fn status_rank(status: &str) -> usize {
    match status {
        "approved" => 6,
        "reviewed" => 5,
        "translated" => 4,
        "draft" => 3,
        "rejected" => 2,
        "new" => 1,
        _ => 0,
    }
}

fn gettext_key(key: &str) -> Option<&'static str> {
    match key {
        "x-po-msgid" => Some("po_msgid"),
        "x-po-msgctxt" => Some("po_msgctxt"),
        "x-po-msgid-plural" => Some("po_msgid_plural"),
        "x-po-plural-index" => Some("gettext_index"),
        "x-po-entry-index" => Some("po_entry_index"),
        "x-po-flags" => Some("flags"),
        "x-po-references" => Some("references"),
        "x-po-previous" => Some("po_previous"),
        _ => None,
    }
}

fn append_gettext(extensions: &mut Vec<(String, String)>, key: &str, value: &str) {
    if key == "po_previous" {
        if let Some((_, previous)) = extensions
            .iter_mut()
            .find(|(candidate, _)| candidate == key)
        {
            previous.push('\n');
            previous.push_str(value);
            return;
        }
    }
    set(extensions, key.into(), value.into());
}

fn gettext(data: &mut Data) {
    let extensions: HashMap<String, String> = data.extensions.iter().cloned().collect();
    data.plural = crate::plural::from_extensions(&extensions);
    if extensions
        .get("flags")
        .is_some_and(|value| value.split(',').any(|flag| flag.trim() == "fuzzy"))
    {
        data.status = TranslationStatus::Draft;
    }
    for comment in &mut data.comments {
        if comment
            .extensions
            .iter()
            .any(|(key, value)| key == "po_comment_kind" && value == "extracted")
        {
            comment.context_key = extensions
                .get("po_msgctxt")
                .cloned()
                .or_else(|| comment.context_key.clone());
        }
    }
}

fn xliff_metadata(node: &Node, data: &mut Data, budget: &mut usize) -> NativeResult<()> {
    match node.local() {
        "note" => {
            let mut comment = Comment::new(node.text());
            retain_attrs(node, "xliff.note", &mut comment.extensions);
            comment.extensions.push((
                "po_comment_kind".into(),
                if node.attr("from") == Some("po-translator") {
                    "translator"
                } else {
                    "extracted"
                }
                .into(),
            ));
            data.comments.push(comment);
        }
        "context" => {
            if let Some(key) = node.attr("context-type").and_then(gettext_key) {
                append_gettext(&mut data.extensions, key, &node.text());
            } else if node.attr("context-type") == Some("x-po-autocomment") {
                let mut comment = Comment::new(node.text());
                comment
                    .extensions
                    .push(("po_comment_kind".into(), "extracted".into()));
                data.comments.push(comment);
            } else {
                let xml = node.serialized_xml(budget)?;
                data.extensions
                    .push((format!("xliff.context.{}", data.extensions.len()), xml));
            }
        }
        "notes" | "context-group" | "prop-group" => {
            for child in node.children() {
                xliff_metadata(child, data, budget)?;
            }
        }
        _ => {
            let xml = node.serialized_xml(budget)?;
            data.extensions.push((
                format!("xliff.module.{}.{}", node.local(), data.extensions.len()),
                xml,
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn payload_reference() -> Node {
        Node {
            name: "ph".into(),
            attrs: vec![("dataRef".into(), "payload".into())],
            content: Vec::new(),
            bytes: 0,
        }
    }

    #[test]
    fn serialized_xml_checks_expansion_before_allocation() {
        let node = Node {
            name: "note".into(),
            attrs: vec![("from".into(), "<&\"'é".into())],
            content: vec![
                Content::Text("<&>\"'é".into()),
                Content::Element(payload_reference()),
            ],
            bytes: 0,
        };
        let expected = node.xml_len();
        let mut budget = expected;
        let xml = node.serialized_xml(&mut budget).unwrap();
        assert_eq!(xml.len(), expected);
        assert_eq!(budget, 0);
        assert!(xml.contains("&lt;&amp;&gt;&quot;&apos;é"));
        assert!(node.serialized_xml(&mut (expected - 1)).is_err());
    }

    #[test]
    fn repeated_inline_payloads_are_bounded_before_projection() {
        let payloads = HashMap::from([("payload".into(), "x".repeat(6000))]);
        let mut node = Node {
            name: "source".into(),
            attrs: Vec::new(),
            content: vec![Content::Element(payload_reference())],
            bytes: 0,
        };
        assert!(inline(Some(&node), false, &[], &payloads, &mut 20_000).is_ok());
        node.content.push(Content::Element(payload_reference()));
        assert!(inline(Some(&node), false, &[], &payloads, &mut 20_000).is_err());
        assert!(inline(Some(&node), false, &[], &HashMap::new(), &mut 20_000).is_ok());
    }

    #[test]
    fn namespace_copies_share_the_inline_budget() {
        let node = payload_reference();
        let namespaces = vec![("xmlns:vendor".into(), "x".repeat(10_000))];
        assert!(inline(
            Some(&node),
            false,
            &namespaces,
            &HashMap::new(),
            &mut 10_000
        )
        .is_err());
    }
}
