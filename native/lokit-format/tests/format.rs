use std::io::{self, BufReader, Cursor, Read, Write};

use lokit_format::{
    format_source, format_source_preserving_comments, parse_reader, parse_str,
    parse_str_with_options, parse_str_with_spans, validate, validate_parsed,
    validate_parsed_with_limit, write_document, AdjacentContext, BaseStructure, CanonicalWriter,
    CodePart, Comment, Data, DiagnosticCode, ErrorCode, Meta, Origin, ParseOptions, Plural,
    PluralCategory, SegmentPart, Tags, TargetData, TargetTags, TextPart, TieData, TieType,
    TranslationStatus, WriteError, INTEGER_MAX, INTEGER_MIN, MAX_LINE_BYTES,
};

fn complete_document() -> BaseStructure {
    let mut source_open = TieData::new("source-open-id", TieType::StrongOpen);
    source_open.attributes = vec![
        ("class".to_owned(), "hero".to_owned()),
        ("empty".to_owned(), String::new()),
    ];
    source_open.attribute_data = "class=hero".to_owned();
    source_open.pair_id = Some("source-pair".to_owned());
    source_open.original_name = Some("strong".to_owned());
    source_open.original_text = Some("<strong>".to_owned());

    let mut source_close = TieData::new("source-close-id", TieType::StrongClose);
    source_close.position = 8;
    source_close.order = 2;
    source_close.pair_id = Some("source-pair".to_owned());
    source_close.original_name = Some("strong".to_owned());
    source_close.original_text = Some("</strong>".to_owned());

    let mut target_code = TieData::new("target-code-id", TieType::Br);
    target_code.pair_id = Some(String::new());
    target_code.original_name = Some("br".to_owned());
    target_code.original_text = Some(String::new());

    let mut localized_code = TieData::new("localized-code-id", TieType::Img);
    localized_code.attributes = vec![("alt".to_owned(), "icon".to_owned())];

    let source_tags = Tags {
        source_tag_map: vec![
            ("source-open-key".to_owned(), source_open),
            ("source-close-key".to_owned(), source_close),
        ],
        target_tag_map: vec![("target-code-key".to_owned(), target_code)],
        source_parts: vec![
            SegmentPart::Code(CodePart::new("source-open-key")),
            SegmentPart::Text(TextPart::new("Hello")),
            SegmentPart::Code(CodePart::new("source-close-key")),
            SegmentPart::Text(TextPart::new(" world")),
        ],
        target_parts: vec![
            SegmentPart::Code(CodePart::new("target-code-key")),
            SegmentPart::Text(TextPart::new("Bonjour monde")),
        ],
    };

    let localized_tags = TargetTags {
        tag_map: vec![("localized-code-key".to_owned(), localized_code)],
        parts: vec![
            SegmentPart::Text(TextPart::new("Hallo")),
            SegmentPart::Code(CodePart::new("localized-code-key")),
        ],
    };

    let mut localized_plural = Plural::new("Formen");
    localized_plural.count = Some(0);
    localized_plural.category = Some(PluralCategory::Zero);
    localized_plural.extensions = vec![("plural-target".to_owned(), String::new())];

    let localized_meta = Meta {
        usage_count: Some(0),
        last_used: Some("2026-07-18T09:10:11Z".to_owned()),
        first_used: Some(String::new()),
        created: Some("2026-01-01".to_owned()),
        updated: Some("2026-07-19".to_owned()),
        max_length: Some(INTEGER_MAX),
        min_length: Some(INTEGER_MIN),
        extensions: vec![("target-meta".to_owned(), "value".to_owned())],
    };
    let localized_comment = Comment {
        context: String::new(),
        timestamp: Some(String::new()),
        origin: Some(Origin::default()),
        context_key: Some(String::new()),
        extensions: vec![("target-comment".to_owned(), String::new())],
    };
    let localized = TargetData {
        text: Some("Hallo".to_owned()),
        status: TranslationStatus::New,
        tags: Some(localized_tags),
        plural: Some(localized_plural),
        meta: localized_meta,
        comments: vec![localized_comment],
        extensions: vec![("target-extension".to_owned(), String::new())],
    };

    let mut plural = Plural::new("Hello worlds");
    plural.count = Some(2);
    plural.category = Some(PluralCategory::Other);
    plural.extensions = vec![("plural".to_owned(), "source".to_owned())];

    let meta = Meta {
        usage_count: Some(7),
        last_used: Some("2026-07-19T12:34:56Z".to_owned()),
        first_used: Some("2025-01-02T03:04:05Z".to_owned()),
        created: Some("2025-01-01T00:00:00Z".to_owned()),
        updated: Some(String::new()),
        max_length: Some(120),
        min_length: Some(0),
        extensions: vec![
            ("quality".to_owned(), "gold".to_owned()),
            ("empty-meta".to_owned(), String::new()),
        ],
    };
    let comment = Comment {
        context: "Translator note".to_owned(),
        timestamp: Some("2026-07-19T12:00:00Z".to_owned()),
        origin: Some(Origin {
            system: Some("cms".to_owned()),
            project: Some("storefront".to_owned()),
            creator_id: Some("user-42".to_owned()),
            extensions: vec![("origin".to_owned(), "human".to_owned())],
        }),
        context_key: Some("homepage.hero".to_owned()),
        extensions: vec![("audience".to_owned(), "public".to_owned())],
    };
    let next_context = AdjacentContext {
        unit_id: Some("next-unit".to_owned()),
        source: Some("Next".to_owned()),
        target: Some(String::new()),
        extensions: vec![("distance".to_owned(), "1".to_owned())],
    };
    let unit = Data {
        source: "Hello world".to_owned(),
        target: Some("Bonjour monde".to_owned()),
        targets: vec![
            ("de-DE".to_owned(), localized),
            ("fr-FR".to_owned(), TargetData::default()),
        ],
        plural: Some(plural),
        tags: Some(source_tags),
        meta,
        status: TranslationStatus::Reviewed,
        comments: vec![comment, Comment::new("")],
        previous_context: Some(AdjacentContext::default()),
        next_context: Some(next_context),
        extensions: vec![
            ("resource".to_owned(), "home".to_owned()),
            ("empty-unit".to_owned(), String::new()),
        ],
    };

    BaseStructure {
        source_locale: "en-US".to_owned(),
        target_locale: Some(String::new()),
        data: vec![
            ("unit/complete".to_owned(), unit),
            (
                "unit/minimal".to_owned(),
                Data {
                    tags: Some(Tags::default()),
                    ..Data::new("")
                },
            ),
        ],
        target_locales: vec!["fr-FR".to_owned(), "de-DE".to_owned()],
        format_version: "0.9".to_owned(),
        export_origin: "lokit-tests".to_owned(),
        export_timestamp: "2026-07-19T13:14:15Z".to_owned(),
        source_language: Some("en".to_owned()),
        target_language: Some(String::new()),
        target_languages: vec!["fr".to_owned(), "de".to_owned()],
        extensions: vec![
            ("input_format".to_owned(), "xliff".to_owned()),
            ("empty-document".to_owned(), String::new()),
        ],
    }
}

#[test]
fn complete_graph_round_trips_canonically() {
    let document = complete_document();
    let source = format_source(&document).expect("complete document should format");
    let parsed = parse_str(&source).expect("canonical source should parse");
    let reformatted = format_source(&parsed).expect("parsed document should format");
    let reparsed = parse_str(&reformatted).expect("reformatted source should parse");

    assert_eq!(parsed, document);
    assert_eq!(reparsed, document);
    assert_eq!(reformatted, source);
    assert!(source.ends_with('\n'));
    assert!(!source.contains("null"));
    assert!(validate(&document).is_empty());
}

#[test]
fn sparse_output_is_exact_and_has_no_null_sentinels() {
    let mut document = BaseStructure::new("");
    document.data.push(("empty".to_owned(), Data::new("")));

    let source = format_source(&document).expect("sparse document should format");
    assert_eq!(
        source,
        "@lokit 1\ndocument {\n  source_locale = \"\"\n}\n\nunit \"empty\" {\n  source = \"\"\n}\n"
    );
    assert!(!source.contains("null"));
    assert_eq!(
        parse_str(&source).expect("sparse source should parse"),
        document
    );
}

#[test]
fn empty_optional_strings_and_blocks_remain_present() {
    let mut document = BaseStructure::new("en");
    document.target_locale = Some(String::new());
    let mut unit = Data::new("source");
    unit.target = Some(String::new());
    unit.tags = Some(Tags::default());
    unit.previous_context = Some(AdjacentContext::default());
    unit.comments.push(Comment {
        origin: Some(Origin::default()),
        ..Comment::new("")
    });
    unit.targets.push((
        "fr".to_owned(),
        TargetData {
            text: Some(String::new()),
            tags: Some(TargetTags::default()),
            ..TargetData::default()
        },
    ));
    document.data.push(("presence".to_owned(), unit));

    let source = format_source(&document).expect("presence document should format");
    let parsed = parse_str(&source).expect("presence source should parse");
    assert_eq!(parsed, document);
    assert!(source.contains("  tags {\n  }"));
    assert!(source.contains("  previous_context {\n  }"));
    assert!(source.contains("    tags {\n    }"));
    assert!(source.contains("    origin {\n    }"));
}

#[test]
fn ordered_lists_preserve_duplicates_and_order() {
    let mut document = BaseStructure::new("en");
    document.target_locales = vec!["fr".to_owned(), "fr".to_owned(), "de".to_owned()];
    document.target_languages = vec!["fr".to_owned(), "fr".to_owned(), "de".to_owned()];

    let parsed = parse_str(&format_source(&document).expect("lists should format"))
        .expect("duplicate list elements are unambiguous");
    assert_eq!(parsed.target_locales, document.target_locales);
    assert_eq!(parsed.target_languages, document.target_languages);
}

#[test]
fn json_strings_comments_unicode_and_crlf_parse() {
    let source = concat!(
        "# leading comment\r\n",
        "@lokit 1\r\n",
        "document {\r\n",
        "  source_locale = \"en\\n\\t\\b\\f\\r\\/\\\\\\\"\\u263a\\ud83c\\udf0d\"\r\n",
        "}\r\n",
        "# between blocks\r\n",
        "unit \"u\\u002f1\" {\r\n",
        "  # nested comment\r\n",
        "  source = \"café\"\r\n",
        "}\r\n",
    );
    let document = parse_str(source).expect("JSON escapes and CRLF should parse");
    assert_eq!(document.source_locale, "en\n\t\u{0008}\u{000c}\r/\\\"☺🌍");
    assert_eq!(document.data[0].0, "u/1");
    assert_eq!(document.data[0].1.source, "café");
}

#[test]
fn canonical_formatting_preserves_full_line_comments_losslessly_and_idempotently() {
    let source = concat!(
        "  # leading comment with trailing bytes \t  \r\n",
        "@lokit 1\r\n",
        "# before document\r\n",
        "document {\r\n",
        "\t # metadata note: café  \t\r\n",
        "  target_locale = \"fr\"\r\n",
        "  source_locale = \"en\"\r\n",
        "}\r\n",
        "# between blocks\r\n",
        "unit \"hello\" {\r\n",
        "  source = \"Hello\"\r\n",
        "  # inside unit\t \r\n",
        "  target = \"Bonjour\"\r\n",
        "}\r\n",
        "# trailing comment without a newline"
    );
    let parsed = parse_str(source).expect("commented source should parse");
    let formatted =
        format_source_preserving_comments(source, &parsed).expect("commented source should format");

    let comments = [
        "  # leading comment with trailing bytes \t  ",
        "# before document",
        "\t # metadata note: café  \t",
        "# between blocks",
        "  # inside unit\t ",
        "# trailing comment without a newline",
    ];
    for comment in comments {
        assert_eq!(
            formatted.matches(comment).count(),
            1,
            "comment was not retained exactly once: {comment:?}"
        );
    }
    let source_locale = formatted
        .find("source_locale")
        .expect("canonical source locale should be present");
    let target_locale = formatted
        .find("target_locale")
        .expect("canonical target locale should be present");
    assert!(source_locale < target_locale);
    assert_eq!(parse_str(&formatted), Ok(parsed.clone()));
    let second = format_source_preserving_comments(&formatted, &parsed)
        .expect("formatted source should format again");
    assert_eq!(second, formatted);
}

#[test]
fn canonical_writer_escapes_every_json_control_character() {
    let mut value = (0_u8..=31).map(char::from).collect::<String>();
    value.push_str(" quote=\" slash=/ backslash=\\ café 🌍");
    let mut document = BaseStructure::new("en");
    document
        .data
        .push(("escaping".to_owned(), Data::new(value)));

    let source = format_source(&document).expect("all controls should be escaped");
    assert!(source.contains("\\u0000"));
    assert!(source.contains("\\u001f"));
    assert!(source.contains("\\b\\t\\n"));
    assert_eq!(
        parse_str(&source).expect("writer output must parse"),
        document
    );
}

#[test]
fn every_enum_token_round_trips() {
    for value in [
        TranslationStatus::New,
        TranslationStatus::Draft,
        TranslationStatus::Translated,
        TranslationStatus::Reviewed,
        TranslationStatus::Approved,
        TranslationStatus::Rejected,
        TranslationStatus::Unknown,
    ] {
        assert_eq!(value.as_str().parse::<TranslationStatus>(), Ok(value));
    }
    for value in [
        PluralCategory::Generic,
        PluralCategory::Zero,
        PluralCategory::One,
        PluralCategory::Two,
        PluralCategory::Few,
        PluralCategory::Many,
        PluralCategory::Other,
    ] {
        assert_eq!(value.as_str().parse::<PluralCategory>(), Ok(value));
    }
    for value in [
        TieType::AOpen,
        TieType::AClose,
        TieType::AbbrOpen,
        TieType::AbbrClose,
        TieType::BOpen,
        TieType::BClose,
        TieType::BdiOpen,
        TieType::BdiClose,
        TieType::BdoOpen,
        TieType::BdoClose,
        TieType::Br,
        TieType::CiteOpen,
        TieType::CiteClose,
        TieType::CodeOpen,
        TieType::CodeClose,
        TieType::DataOpen,
        TieType::DataClose,
        TieType::DfnOpen,
        TieType::DfnClose,
        TieType::EmOpen,
        TieType::EmClose,
        TieType::IOpen,
        TieType::IClose,
        TieType::Img,
        TieType::KbdOpen,
        TieType::KbdClose,
        TieType::MarkOpen,
        TieType::MarkClose,
        TieType::QOpen,
        TieType::QClose,
        TieType::RpOpen,
        TieType::RpClose,
        TieType::RtOpen,
        TieType::RtClose,
        TieType::RubyOpen,
        TieType::RubyClose,
        TieType::SOpen,
        TieType::SClose,
        TieType::SampOpen,
        TieType::SampClose,
        TieType::SmallOpen,
        TieType::SmallClose,
        TieType::SpanOpen,
        TieType::SpanClose,
        TieType::StrongOpen,
        TieType::StrongClose,
        TieType::SubOpen,
        TieType::SubClose,
        TieType::SupOpen,
        TieType::SupClose,
        TieType::TimeOpen,
        TieType::TimeClose,
        TieType::UOpen,
        TieType::UClose,
        TieType::VarOpen,
        TieType::VarClose,
        TieType::Wbr,
        TieType::CustomOpen,
        TieType::CustomClose,
        TieType::CustomStandalone,
    ] {
        assert_eq!(value.as_str().parse::<TieType>(), Ok(value));
    }
    assert!("not-a-status".parse::<TranslationStatus>().is_err());
    assert!("not-a-category".parse::<PluralCategory>().is_err());
    assert!("not-a-tie".parse::<TieType>().is_err());
}

#[test]
fn streaming_and_full_writers_are_byte_identical() {
    let document = complete_document();
    let full = format_source(&document).expect("full formatting should succeed");
    let mut bytes = Vec::new();
    {
        let mut writer = CanonicalWriter::new(&mut bytes);
        writer.start(&document).expect("header should write");
        for (unit_id, data) in &document.data {
            writer.write_unit(unit_id, data).expect("unit should write");
        }
        writer.finish().expect("writer should finish");
        assert!(writer.is_finished());
    }
    assert_eq!(bytes, full.as_bytes());
}

#[test]
fn streaming_writer_rejects_duplicate_ids_and_state_misuse() {
    let mut output = Vec::new();
    let mut writer = CanonicalWriter::new(&mut output);
    assert!(matches!(
        writer.write_unit("u", &Data::new("s")),
        Err(WriteError::InvalidState { .. })
    ));
    writer
        .start(&BaseStructure::new("en"))
        .expect("header should write");
    writer
        .write_unit("u", &Data::new("one"))
        .expect("first unit should write");
    assert!(matches!(
        writer.write_unit("u", &Data::new("two")),
        Err(WriteError::DuplicateKey { .. })
    ));
    writer.finish().expect("writer should finish");
    assert!(matches!(
        writer.finish(),
        Err(WriteError::InvalidState { .. })
    ));
    assert!(matches!(
        writer.write_unit("v", &Data::new("three")),
        Err(WriteError::InvalidState { .. })
    ));
}

#[test]
fn ordinary_streaming_does_not_collect_spans_but_opt_in_does() {
    let source = format_source(&complete_document()).expect("fixture should format");
    let mut ordinary =
        lokit_format::StreamingReader::new(BufReader::new(Cursor::new(source.as_bytes())))
            .expect("ordinary reader should start");
    assert!(ordinary.source_map().entries().is_empty());
    ordinary.next_unit().expect("unit should parse");
    assert!(ordinary.source_map().entries().is_empty());

    let mut located =
        lokit_format::StreamingReader::with_spans(BufReader::new(Cursor::new(source.as_bytes())))
            .expect("located reader should start");
    assert!(!located.source_map().entries().is_empty());
    located.next_unit().expect("unit should parse");
    let first_map = located.take_source_map();
    assert!(first_map.span("units[0].source").is_some());
    assert!(located.source_map().entries().is_empty());
    located.next_unit().expect("second unit should parse");
    assert!(located.source_map().span("units[1].source").is_some());
}

#[test]
fn streaming_reader_exposes_header_and_units_incrementally() {
    let document = complete_document();
    let source = format_source(&document).expect("fixture should format");
    let mut reader =
        lokit_format::StreamingReader::new(BufReader::new(Cursor::new(source.as_bytes())))
            .expect("stream should open");
    assert_eq!(
        reader.document_header().source_locale,
        document.source_locale
    );
    assert!(reader.document_header().data.is_empty());
    assert_eq!(
        reader.next_unit().expect("first unit should parse"),
        Some(document.data[0].clone())
    );
    assert_eq!(
        reader.next_unit().expect("second unit should parse"),
        Some(document.data[1].clone())
    );
    assert_eq!(reader.next_unit().expect("EOF should parse"), None);
    assert_eq!(reader.next_unit().expect("EOF should remain stable"), None);
}

#[test]
fn tag_integrity_diagnostics_are_located_without_rejecting_parse() {
    let source = concat!(
        "@lokit 1\n",
        "document {\n",
        "  source_locale = \"en\"\n",
        "}\n",
        "unit \"u\" {\n",
        "  source = \"expected\"\n",
        "  tags {\n",
        "    source_tag \"open\" {\n",
        "      id = \"open-id\"\n",
        "      type = strong.open\n",
        "      pair_id = \"pair\"\n",
        "    }\n",
        "    source_tag \"unused\" {\n",
        "      id = \"unused-id\"\n",
        "      type = br.standalone\n",
        "    }\n",
        "    source_parts {\n",
        "      code = \"missing\"\n",
        "      code = \"open\"\n",
        "      code = \"open\"\n",
        "      text = \"different\"\n",
        "    }\n",
        "  }\n",
        "}\n",
    );
    let parsed = parse_str_with_spans(source).expect("dangling refs are syntactically valid");
    let diagnostics = validate_parsed(&parsed);
    for code in [
        DiagnosticCode::DanglingTagReference,
        DiagnosticCode::DuplicateTagReference,
        DiagnosticCode::UnreferencedTag,
        DiagnosticCode::IncompleteTagPair,
        DiagnosticCode::PartsTextMismatch,
    ] {
        let diagnostic = diagnostics
            .iter()
            .find(|diagnostic| diagnostic.code == code)
            .unwrap_or_else(|| panic!("missing diagnostic {code:?}: {diagnostics:?}"));
        assert!(
            diagnostic.span.is_some(),
            "{diagnostic:?} should be located"
        );
    }
}

#[test]
fn located_validation_stops_at_the_requested_diagnostic_limit() {
    let mut data = Data::new("");
    data.tags = Some(Tags {
        source_tag_map: (0..4_096)
            .map(|index| {
                (
                    format!("unused-{index}"),
                    TieData::new(format!("unused-id-{index}"), TieType::Br),
                )
            })
            .collect(),
        ..Tags::default()
    });
    let mut document = BaseStructure::new("en");
    document.data.push(("diagnostic-heavy".to_owned(), data));
    let source = format_source(&document).expect("fixture should format");
    let parsed = parse_str_with_spans(&source).expect("fixture should parse");

    assert!(validate_parsed_with_limit(&parsed, 0).is_empty());
    assert_eq!(validate_parsed_with_limit(&parsed, 1).len(), 1);
    let capped = validate_parsed_with_limit(&parsed, 200);
    assert_eq!(capped.len(), 200);
    assert!(capped.iter().all(|item| item.span.is_some()));
    assert_eq!(validate_parsed(&parsed).len(), 4_096);
}

#[test]
fn duplicate_constructed_map_is_diagnosed_and_not_serialized() {
    let mut document = BaseStructure::new("en");
    document.data = vec![
        ("same".to_owned(), Data::new("one")),
        ("same".to_owned(), Data::new("two")),
    ];
    assert!(validate(&document)
        .iter()
        .any(|diagnostic| diagnostic.code == DiagnosticCode::DuplicateUnitId));
    assert!(matches!(
        format_source(&document),
        Err(WriteError::DuplicateKey { .. })
    ));
    let mut output = Vec::new();
    assert!(matches!(
        write_document(&mut output, &document),
        Err(WriteError::DuplicateKey { .. })
    ));
    assert!(output.is_empty());
}

#[test]
fn i64_boundaries_round_trip_and_overflow_is_located() {
    let mut document = BaseStructure::new("en");
    let mut unit = Data::new("source");
    unit.meta.min_length = Some(INTEGER_MIN);
    unit.meta.max_length = Some(INTEGER_MAX);
    document.data.push(("bounds".to_owned(), unit));
    let parsed = parse_str(&format_source(&document).expect("i64 boundaries should format"))
        .expect("i64 boundaries should parse");
    assert_eq!(parsed, document);

    let overflow = concat!(
        "@lokit 1\n",
        "document {\n  source_locale = \"en\"\n}\n",
        "unit \"u\" {\n  source = \"s\"\n  meta {\n",
        "    max_length = 9223372036854775808\n  }\n}\n",
    );
    let error = parse_str(overflow).expect_err("overflow must fail");
    assert_eq!(error.code, ErrorCode::InvalidInteger);
    assert_eq!(error.line(), 8);
}

#[test]
fn canonical_line_limit_matches_default_parser_limit() {
    const SOURCE_PREFIX_BYTES: usize = 2 + 6 + 3 + 2;
    let exact_value = "x".repeat(MAX_LINE_BYTES - SOURCE_PREFIX_BYTES);
    let mut document = BaseStructure::new("en");
    document.data.push(("u".to_owned(), Data::new(exact_value)));
    let source = format_source(&document).expect("exactly bounded line should format");
    assert_eq!(
        parse_str(&source).expect("default parser must accept writer boundary"),
        document
    );

    let oversized_value = "x".repeat(MAX_LINE_BYTES - SOURCE_PREFIX_BYTES + 1);
    document.data[0].1.source = oversized_value;
    assert!(matches!(
        format_source(&document),
        Err(WriteError::LineTooLong {
            maximum: MAX_LINE_BYTES,
            ..
        })
    ));

    const LIST_PREFIX_BYTES: usize = 2 + 14 + 5 + 2;
    let mut list_document = BaseStructure::new("en");
    list_document.target_locales = vec!["x".repeat(MAX_LINE_BYTES - LIST_PREFIX_BYTES)];
    let list_source =
        format_source(&list_document).expect("exactly bounded string-list line should format");
    assert_eq!(
        parse_str(&list_source).expect("default parser must accept list boundary"),
        list_document
    );
    list_document.target_locales[0].push('x');
    assert!(matches!(
        format_source(&list_document),
        Err(WriteError::LineTooLong {
            maximum: MAX_LINE_BYTES,
            ..
        })
    ));
}

#[test]
fn configured_line_and_nesting_limits_are_enforced() {
    let line_source = "@lokit 1\ndocument {\n  source_locale = \"english\"\n}\n";
    let error = parse_str_with_options(
        line_source,
        ParseOptions {
            max_line_bytes: 16,
            max_nesting: 16,
        },
    )
    .expect_err("long line should fail");
    assert_eq!(error.code, ErrorCode::LineTooLong);

    let nested = concat!(
        "@lokit 1\n",
        "document {\n  source_locale = \"en\"\n}\n",
        "unit \"u\" {\n  source = \"s\"\n  plural {\n",
        "    variant = \"v\"\n  }\n}\n",
    );
    let error = parse_str_with_options(
        nested,
        ParseOptions {
            max_line_bytes: MAX_LINE_BYTES,
            max_nesting: 1,
        },
    )
    .expect_err("deep line should fail");
    assert_eq!(error.code, ErrorCode::NestingLimit);
}

#[test]
fn line_limit_span_uses_the_previous_unicode_scalar_boundary() {
    let source = concat!(
        "@lokit 1\n",
        "document {\n",
        "  source_locale = \"é🌍\"\n",
        "}\n",
    );
    let line_start = source
        .find("  source_locale")
        .expect("fixture line must exist");
    let prefix = "  source_locale = \"é";
    let error = parse_str_with_options(
        source,
        ParseOptions {
            // This falls two bytes into the four-byte globe scalar.
            max_line_bytes: prefix.len() + 2,
            max_nesting: 16,
        },
    )
    .expect_err("a line over the byte limit must fail");

    assert_eq!(error.code, ErrorCode::LineTooLong);
    assert_eq!(error.line(), 3);
    assert_eq!(error.column(), prefix.chars().count() + 1);
    assert_eq!(error.span.start.byte, line_start + prefix.len());
    assert!(source.is_char_boundary(error.span.start.byte));
}

#[test]
fn invalid_utf8_column_counts_prior_unicode_scalars() {
    let mut bytes = b"@lokit 1\ndocument {\n  source_locale = \"".to_vec();
    bytes.extend_from_slice("é🌍".as_bytes());
    let invalid_byte = bytes.len();
    bytes.push(0xff);
    bytes.extend_from_slice(b"\"\n}\n");
    let error = parse_reader(Cursor::new(bytes)).expect_err("invalid UTF-8 must fail");
    assert_eq!(error.code, ErrorCode::InvalidUtf8);
    assert_eq!(error.line(), 3);
    assert_eq!(
        error.column(),
        "  source_locale = \"é🌍".chars().count() + 1
    );
    assert_eq!(error.span.start.byte, invalid_byte);
}

#[test]
fn tabs_are_classified_by_context() {
    let with_comment_tabs = concat!(
        "\t# tab before a full-line comment is non-semantic\n",
        "@lokit 1\n",
        "document {\n",
        "  \t# tabs within comment indentation are accepted\n",
        "  source_locale = \"en\"\n",
        "  # a tab in the comment body\tis accepted\n",
        "}\n",
    );
    assert_eq!(
        parse_str(with_comment_tabs)
            .expect("tabs anywhere on a full-line comment should be ignored")
            .source_locale,
        "en"
    );

    let raw_string_tab = "@lokit 1\ndocument {\n  source_locale = \"en\tUS\"\n}\n";
    let error = parse_str(raw_string_tab).expect_err("raw JSON tabs must be escaped");
    assert_eq!(error.code, ErrorCode::InvalidString);

    let indentation_tab = "@lokit 1\ndocument {\n\tsource_locale = \"en\"\n}\n";
    let error = parse_str(indentation_tab).expect_err("statement indentation cannot use tabs");
    assert_eq!(error.code, ErrorCode::InvalidIndentation);
}

#[test]
fn reader_and_writer_io_failures_are_typed() {
    struct BrokenReader;

    impl Read for BrokenReader {
        fn read(&mut self, _buffer: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("read failed"))
        }
    }

    struct BrokenWriter;

    impl Write for BrokenWriter {
        fn write(&mut self, _buffer: &[u8]) -> io::Result<usize> {
            Err(io::Error::other("write failed"))
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("flush failed"))
        }
    }

    let parse_error = parse_reader(BrokenReader).expect_err("read failure must be preserved");
    assert_eq!(parse_error.code, ErrorCode::Io);
    assert!(parse_error.to_string().contains("read failed"));

    let mut writer = CanonicalWriter::new(BrokenWriter);
    assert!(matches!(
        writer.start(&BaseStructure::new("en")),
        Err(WriteError::Io(_))
    ));
    assert!(matches!(
        writer.start(&BaseStructure::new("en")),
        Err(WriteError::InvalidState {
            state: "failed",
            ..
        })
    ));
}

#[test]
fn malformed_documents_return_stable_error_codes() {
    let cases = [
        ("", ErrorCode::InvalidMagic),
        (
            "@lokit 2\ndocument {\n  source_locale = \"en\"\n}\n",
            ErrorCode::UnsupportedVersion,
        ),
        ("@lokit 1\n", ErrorCode::MissingDocument),
        ("@lokit 1\ndocument {\n}\n", ErrorCode::MissingRequiredField),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n  source_locale = \"fr\"\n}\n",
            ErrorCode::Duplicate,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n  extension \"x\" = \"1\"\n  extension \"x\" = \"2\"\n}\n",
            ErrorCode::Duplicate,
        ),
        (
            "@lokit 1\ndocument {\n  unknown = \"x\"\n}\n",
            ErrorCode::UnknownField,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n}\n",
            ErrorCode::MissingRequiredField,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n  plural {\n  }\n}\n",
            ErrorCode::MissingRequiredField,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n  comment {\n  }\n}\n",
            ErrorCode::MissingRequiredField,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n  tags {\n    source_tag \"x\" {\n      type = br.standalone\n    }\n  }\n}\n",
            ErrorCode::MissingRequiredField,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n  target = null\n}\n",
            ErrorCode::InvalidValue,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n  status = impossible\n}\n",
            ErrorCode::InvalidEnum,
        ),
        (
            "@lokit 1\ndocument {\n source_locale = \"en\"\n}\n",
            ErrorCode::InvalidIndentation,
        ),
        (
            "@lokit 1\ndocument {\n\tsource_locale = \"en\"\n}\n",
            ErrorCode::InvalidIndentation,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n",
            ErrorCode::UnclosedBlock,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\\x\"\n}\n",
            ErrorCode::InvalidString,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"unterminated\n",
            ErrorCode::InvalidString,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"\\ud800\"\n}\n",
            ErrorCode::InvalidString,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"\\udc00\"\n}\n",
            ErrorCode::InvalidString,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"\\u🌍00\"\n}\n",
            ErrorCode::InvalidString,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n}\nunit \"u\" {\n  source = \"s\"\n}\n",
            ErrorCode::Duplicate,
        ),
        (
            "@lokit 1\ndocument {\n  source_locale = \"en\"\n}\nunit \"u\" {\n  source = \"s\"\n  target \"fr\" {\n  }\n  target \"fr\" {\n  }\n}\n",
            ErrorCode::Duplicate,
        ),
    ];

    for (source, expected) in cases {
        let result = std::panic::catch_unwind(|| parse_str(source));
        let parsed = result.unwrap_or_else(|_| panic!("parser panicked for {source:?}"));
        let error = parsed.unwrap_err();
        assert_eq!(error.code, expected, "wrong error for {source:?}: {error}");
        assert!(error.line() >= 1);
        assert!(error.column() >= 1);
    }
}
