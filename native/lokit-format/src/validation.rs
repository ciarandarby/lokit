use std::collections::HashMap;

use crate::diagnostic::{Diagnostic, DiagnosticCode, DiagnosticSeverity, SourceMap, SourceSpan};
use crate::model::{BaseStructure, Data, SegmentPart, Tags, TargetTags, TieData};
use crate::parser::ParsedDocument;

/// Validate a materialized document without source locations.
pub fn validate(document: &BaseStructure) -> Vec<Diagnostic> {
    validate_document(document, None, usize::MAX)
}

/// Validate a parsed document and attach the most precise recorded source span
/// to every diagnostic.
pub fn validate_parsed(document: &ParsedDocument) -> Vec<Diagnostic> {
    validate_document(&document.document, Some(&document.source_map), usize::MAX)
}

/// Validate a parsed document while constructing at most `limit` diagnostics.
///
/// Validation stops traversing once the limit is reached. This is intended for
/// interactive consumers such as language servers that must bound both output
/// and work for adversarial, diagnostic-heavy documents.
pub fn validate_parsed_with_limit(document: &ParsedDocument, limit: usize) -> Vec<Diagnostic> {
    validate_document(&document.document, Some(&document.source_map), limit)
}

/// Validate one streamed unit without retaining the rest of the document.
pub fn validate_unit(unit_index: usize, unit_id: &str, data: &Data) -> Vec<Diagnostic> {
    let mut diagnostics = DiagnosticCollector::unlimited();
    validate_data(unit_index, unit_id, data, None, &mut diagnostics);
    diagnostics.into_values()
}

/// Validate one streamed unit and locate diagnostics in the reader's
/// incrementally populated source map.
pub fn validate_unit_with_spans(
    unit_index: usize,
    unit_id: &str,
    data: &Data,
    source_map: &SourceMap,
) -> Vec<Diagnostic> {
    let mut diagnostics = DiagnosticCollector::unlimited();
    validate_data(
        unit_index,
        unit_id,
        data,
        Some(source_map),
        &mut diagnostics,
    );
    diagnostics.into_values()
}

fn validate_document(
    document: &BaseStructure,
    source_map: Option<&SourceMap>,
    limit: usize,
) -> Vec<Diagnostic> {
    let mut diagnostics = DiagnosticCollector::new(limit);
    duplicate_pairs(
        &document.extensions,
        "document.extensions",
        source_map,
        &mut diagnostics,
    );

    let mut units = HashMap::<&str, usize>::new();
    for (index, (unit_id, data)) in document.data.iter().enumerate() {
        if diagnostics.is_full() {
            break;
        }
        let path = format!("units[{index}]");
        if let Some(first_index) = units.insert(unit_id, index) {
            push(
                &mut diagnostics,
                DiagnosticCode::DuplicateUnitId,
                DiagnosticSeverity::Error,
                format!("unit id {unit_id:?} duplicates units[{first_index}]"),
                path,
                source_map,
            );
        }
        validate_data(index, unit_id, data, source_map, &mut diagnostics);
    }
    diagnostics.into_values()
}

struct DiagnosticCollector {
    limit: usize,
    values: Vec<Diagnostic>,
}

impl DiagnosticCollector {
    const fn new(limit: usize) -> Self {
        Self {
            limit,
            values: Vec::new(),
        }
    }

    const fn unlimited() -> Self {
        Self::new(usize::MAX)
    }

    fn is_full(&self) -> bool {
        self.values.len() >= self.limit
    }

    fn into_values(self) -> Vec<Diagnostic> {
        self.values
    }

    fn push(&mut self, diagnostic: Diagnostic) {
        if !self.is_full() {
            self.values.push(diagnostic);
        }
    }
}

fn validate_data(
    unit_index: usize,
    unit_id: &str,
    data: &Data,
    source_map: Option<&SourceMap>,
    diagnostics: &mut DiagnosticCollector,
) {
    if diagnostics.is_full() {
        return;
    }
    let path = format!("units[{unit_index}]");
    duplicate_pairs(
        &data.targets,
        &format!("{path}.targets"),
        source_map,
        diagnostics,
    );
    duplicate_pairs(
        &data.extensions,
        &format!("{path}.extensions"),
        source_map,
        diagnostics,
    );
    if let Some(plural) = &data.plural {
        duplicate_pairs(
            &plural.extensions,
            &format!("{path}.plural.extensions"),
            source_map,
            diagnostics,
        );
    }
    duplicate_pairs(
        &data.meta.extensions,
        &format!("{path}.meta.extensions"),
        source_map,
        diagnostics,
    );
    validate_comments(
        &data.comments,
        &format!("{path}.comments"),
        source_map,
        diagnostics,
    );
    if let Some(context) = &data.previous_context {
        duplicate_pairs(
            &context.extensions,
            &format!("{path}.previous_context.extensions"),
            source_map,
            diagnostics,
        );
    }
    if let Some(context) = &data.next_context {
        duplicate_pairs(
            &context.extensions,
            &format!("{path}.next_context.extensions"),
            source_map,
            diagnostics,
        );
    }

    if let Some(tags) = &data.tags {
        validate_tags(tags, data, &path, source_map, diagnostics);
    }
    for (target_index, (locale, target)) in data.targets.iter().enumerate() {
        if diagnostics.is_full() {
            return;
        }
        let target_path = format!("{path}.targets[{target_index}]");
        duplicate_pairs(
            &target.extensions,
            &format!("{target_path}.extensions"),
            source_map,
            diagnostics,
        );
        duplicate_pairs(
            &target.meta.extensions,
            &format!("{target_path}.meta.extensions"),
            source_map,
            diagnostics,
        );
        validate_comments(
            &target.comments,
            &format!("{target_path}.comments"),
            source_map,
            diagnostics,
        );
        if let Some(plural) = &target.plural {
            duplicate_pairs(
                &plural.extensions,
                &format!("{target_path}.plural.extensions"),
                source_map,
                diagnostics,
            );
        }
        if let Some(tags) = &target.tags {
            validate_target_tags(
                tags,
                target.text.as_deref(),
                &target_path,
                locale,
                unit_id,
                source_map,
                diagnostics,
            );
        }
    }
}

fn validate_comments(
    comments: &[crate::model::Comment],
    path: &str,
    source_map: Option<&SourceMap>,
    diagnostics: &mut DiagnosticCollector,
) {
    for (index, comment) in comments.iter().enumerate() {
        if diagnostics.is_full() {
            return;
        }
        let comment_path = format!("{path}[{index}]");
        duplicate_pairs(
            &comment.extensions,
            &format!("{comment_path}.extensions"),
            source_map,
            diagnostics,
        );
        if let Some(origin) = &comment.origin {
            duplicate_pairs(
                &origin.extensions,
                &format!("{comment_path}.origin.extensions"),
                source_map,
                diagnostics,
            );
        }
    }
}

fn validate_tags(
    tags: &Tags,
    data: &Data,
    path: &str,
    source_map: Option<&SourceMap>,
    diagnostics: &mut DiagnosticCollector,
) {
    if diagnostics.is_full() {
        return;
    }
    duplicate_pairs(
        &tags.source_tag_map,
        &format!("{path}.tags.source_tag_map"),
        source_map,
        diagnostics,
    );
    duplicate_pairs(
        &tags.target_tag_map,
        &format!("{path}.tags.target_tag_map"),
        source_map,
        diagnostics,
    );
    validate_tie_attributes(
        &tags.source_tag_map,
        &format!("{path}.tags.source_tag_map"),
        source_map,
        diagnostics,
    );
    validate_tie_attributes(
        &tags.target_tag_map,
        &format!("{path}.tags.target_tag_map"),
        source_map,
        diagnostics,
    );
    validate_segment(
        &data.source,
        &tags.source_tag_map,
        &tags.source_parts,
        &format!("{path}.tags.source_tag_map"),
        &format!("{path}.tags.source_parts"),
        source_map,
        diagnostics,
    );
    if let Some(target) = data.target.as_deref() {
        validate_segment(
            target,
            &tags.target_tag_map,
            &tags.target_parts,
            &format!("{path}.tags.target_tag_map"),
            &format!("{path}.tags.target_parts"),
            source_map,
            diagnostics,
        );
    } else if !tags.target_parts.is_empty() || !tags.target_tag_map.is_empty() {
        push(
            diagnostics,
            DiagnosticCode::PartsTextMismatch,
            DiagnosticSeverity::Warning,
            "target tags exist but the unit has no target text",
            format!("{path}.tags.target_parts"),
            source_map,
        );
    }
}

#[allow(clippy::too_many_arguments)]
fn validate_target_tags(
    tags: &TargetTags,
    text: Option<&str>,
    path: &str,
    locale: &str,
    unit_id: &str,
    source_map: Option<&SourceMap>,
    diagnostics: &mut DiagnosticCollector,
) {
    if diagnostics.is_full() {
        return;
    }
    duplicate_pairs(
        &tags.tag_map,
        &format!("{path}.tags.tag_map"),
        source_map,
        diagnostics,
    );
    validate_tie_attributes(
        &tags.tag_map,
        &format!("{path}.tags.tag_map"),
        source_map,
        diagnostics,
    );
    if let Some(text) = text {
        validate_segment(
            text,
            &tags.tag_map,
            &tags.parts,
            &format!("{path}.tags.tag_map"),
            &format!("{path}.tags.parts"),
            source_map,
            diagnostics,
        );
    } else if !tags.parts.is_empty() || !tags.tag_map.is_empty() {
        push(
            diagnostics,
            DiagnosticCode::PartsTextMismatch,
            DiagnosticSeverity::Warning,
            format!("target {locale:?} in unit {unit_id:?} has tags but no text"),
            format!("{path}.tags.parts"),
            source_map,
        );
    }
}

fn validate_tie_attributes(
    tags: &[(String, TieData)],
    path: &str,
    source_map: Option<&SourceMap>,
    diagnostics: &mut DiagnosticCollector,
) {
    for (index, (_, tie)) in tags.iter().enumerate() {
        if diagnostics.is_full() {
            return;
        }
        duplicate_pairs(
            &tie.attributes,
            &format!("{path}[{index}].attributes"),
            source_map,
            diagnostics,
        );
    }
}

fn validate_segment(
    text: &str,
    tags: &[(String, TieData)],
    parts: &[SegmentPart],
    map_path: &str,
    parts_path: &str,
    source_map: Option<&SourceMap>,
    diagnostics: &mut DiagnosticCollector,
) {
    if diagnostics.is_full() {
        return;
    }
    let map = tags
        .iter()
        .enumerate()
        .map(|(index, (key, tag))| (key.as_str(), (index, tag)))
        .collect::<HashMap<_, _>>();
    let mut references = HashMap::<&str, usize>::new();
    let mut plain_text = String::new();
    let mut pair_state = HashMap::<&str, PairState>::new();

    for (part_index, part) in parts.iter().enumerate() {
        if diagnostics.is_full() {
            return;
        }
        match part {
            SegmentPart::Text(part) => plain_text.push_str(&part.value),
            SegmentPart::Code(part) => {
                let part_path = format!("{parts_path}[{part_index}]");
                if let Some(first_index) = references.insert(&part.r#ref, part_index) {
                    push(
                        diagnostics,
                        DiagnosticCode::DuplicateTagReference,
                        DiagnosticSeverity::Error,
                        format!(
                            "tag reference {:?} duplicates part {first_index}",
                            part.r#ref
                        ),
                        part_path.clone(),
                        source_map,
                    );
                }
                let Some((_, tag)) = map.get(part.r#ref.as_str()) else {
                    push(
                        diagnostics,
                        DiagnosticCode::DanglingTagReference,
                        DiagnosticSeverity::Error,
                        format!(
                            "tag reference {:?} has no matching tag-map entry",
                            part.r#ref
                        ),
                        part_path,
                        source_map,
                    );
                    continue;
                };
                if let Some(pair_id) = tag.pair_id.as_deref() {
                    let state = pair_state.entry(pair_id).or_default();
                    state.open |= tag.r#type.is_open();
                    state.close |= tag.r#type.is_close();
                }
            }
        }
    }

    for (tag_index, (key, _)) in tags.iter().enumerate() {
        if diagnostics.is_full() {
            return;
        }
        if !references.contains_key(key.as_str()) {
            push(
                diagnostics,
                DiagnosticCode::UnreferencedTag,
                DiagnosticSeverity::Error,
                format!("tag-map entry {key:?} is not referenced by parts"),
                format!("{map_path}[{tag_index}]"),
                source_map,
            );
        }
    }
    for (pair_id, state) in pair_state {
        if diagnostics.is_full() {
            return;
        }
        if state.open != state.close {
            push(
                diagnostics,
                DiagnosticCode::IncompleteTagPair,
                DiagnosticSeverity::Error,
                format!("inline tag pair {pair_id:?} is incomplete"),
                parts_path.to_owned(),
                source_map,
            );
        }
    }
    if plain_text != text {
        push(
            diagnostics,
            DiagnosticCode::PartsTextMismatch,
            DiagnosticSeverity::Warning,
            "text parts do not reproduce the associated plain text",
            parts_path.to_owned(),
            source_map,
        );
    }
}

#[derive(Default)]
struct PairState {
    open: bool,
    close: bool,
}

fn duplicate_pairs<T>(
    values: &[(String, T)],
    path: &str,
    source_map: Option<&SourceMap>,
    diagnostics: &mut DiagnosticCollector,
) {
    let mut first = HashMap::<&str, usize>::new();
    for (index, (key, _)) in values.iter().enumerate() {
        if diagnostics.is_full() {
            return;
        }
        if let Some(first_index) = first.insert(key, index) {
            push(
                diagnostics,
                DiagnosticCode::DuplicateMapKey,
                DiagnosticSeverity::Error,
                format!("map key {key:?} duplicates index {first_index}"),
                format!("{path}[{index}]"),
                source_map,
            );
        }
    }
}

fn push(
    diagnostics: &mut DiagnosticCollector,
    code: DiagnosticCode,
    severity: DiagnosticSeverity,
    message: impl Into<String>,
    path: String,
    source_map: Option<&SourceMap>,
) {
    if diagnostics.is_full() {
        return;
    }
    diagnostics.push(Diagnostic {
        code,
        severity,
        message: message.into(),
        span: source_map.and_then(|map| closest_span(map, &path)),
        path,
    });
}

fn closest_span(source_map: &SourceMap, path: &str) -> Option<SourceSpan> {
    if let Some(span) = source_map.span(path) {
        return Some(span.clone());
    }
    let mut parent = path;
    while let Some(end) = parent.rfind(['.', '[']) {
        parent = &parent[..end];
        if let Some(span) = source_map.span(parent) {
            return Some(span.clone());
        }
    }
    None
}
