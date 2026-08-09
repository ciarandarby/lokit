# Lokit Interchange Format 1

Status: project specification for `.lokit` schema version 1.

Implementation boundaries, performance decisions, and verification layers are
recorded in [`lokit-architecture.md`](lokit-architecture.md).

## Purpose

The Lokit Interchange Format is Lokit's lossless, streamable representation of
the documented `BaseStructure` domain. It covers every current model field and
presence state, subject to the explicit integer and line-size bounds below. It
is designed for localization data rather than for arbitrary JSON values.

The format borrows several useful ideas from TOON: one logical construct per
line, fixed indentation, deterministic text encoding, and omission of absent
or default data. It is not TOON and does not implement the TOON data model,
array headers, tabular rows, delimiter rules, or `null` values.

The design goals are:

- exact round trips for every field and value inside the v1 representable domain;
- bounded, one-pass parsing and writing by translation unit;
- readable diffs and useful editor tooling;
- an unambiguous sparse representation with no null literal;
- deterministic UTF-8 output and strict, located diagnostics;
- independent schema versioning and forward evolution through extensions.

## Encoding

A document is UTF-8 text. Canonical writers use LF (`U+000A`) line endings,
two spaces for each indentation level, no trailing whitespace, and one final
LF. There is no blank line between the magic line and `document`. When units
exist, canonical output has exactly one blank line between `document` and the
first unit and exactly one blank line between consecutive units. It has no
blank lines inside blocks. A unit-free document ends immediately after the
document block's final LF.

Parsers accept blank lines and full-line comments whose first non-space-or-tab
character is `#`, including before the magic line. These source comments are
non-semantic and a canonical formatter removes them; use structured `comment`
blocks for content that must round-trip. Tabs may occur anywhere on such a
full-line comment, including before `#`. Tabs are not valid indentation on a
syntactic line. An unescaped tab inside a quoted string is invalid JSON string
syntax; use the `\t` escape to represent `U+0009`.

The first significant line is the magic and schema-major version:

```lokit
@lokit 1
```

The schema version belongs to the file envelope. It is independent of the
`format_version` field stored in `BaseStructure`.

## Values

Strings are finite sequences of Unicode scalar values: `U+0000` through
`U+D7FF` and `U+E000` through `U+10FFFF`. Documents encode those scalars as
strict UTF-8, without Unicode normalization. Strings use JSON double-quoted
syntax and escapes. Supplementary scalars may use a valid JSON UTF-16
surrogate pair such as `\uD83C\uDF0D`; lone surrogates are invalid and are not
part of the v1 string domain. Control scalars use JSON escapes in canonical
output and may not occur raw inside a quoted string.

Required strings are always written, including empty strings. Optional strings
are omitted only when their model value is `None`; an optional empty string is
therefore written as `""`.

Integers use base-10 signed 64-bit notation (`-9223372036854775808` through
`9223372036854775807`). Enum values use their lowercase Lokit values,
for example `translated`, `other`, and `strong.open`. Ordered string tuples use
a JSON-compatible string array. There are no boolean, floating-point, object,
or `null` value literals.

An absent optional value has no statement. An optional object that exists but
has only default fields is represented by an explicit empty block. This keeps,
for example, `tags is None` distinct from `tags == Tags()`.

### Representable model domain

Python's `int` and `str` types are intrinsically unbounded, while a safe
streaming interchange parser must impose finite resource limits. Python
strings containing lone surrogate code points are also broader than the
Unicode-scalar string domain representable in strict UTF-8. Version 1 is
lossless for every current `BaseStructure` field when every string is a
Unicode-scalar sequence, each integer is in the signed 64-bit range, and every
canonically escaped physical line is at most 1 MiB (1,048,576 UTF-8 bytes,
excluding its terminating LF). Values outside that domain are rejected
explicitly and atomic exporters leave any existing destination unchanged. The
format does not silently clamp, truncate, split, normalize, or replace them.

## Document structure

Document metadata precedes all units so a streaming reader can expose it before
the first unit is consumed.

```lokit
@lokit 1
document {
  source_locale = "en-US"
  target_locale = "fr-FR"
  target_locales = ["fr-FR"]
  source_language = "en"
  target_language = "fr"
}

unit "welcome.title" {
  source = "Welcome"
  target = "Bienvenue"
  status = translated
}
```

`document` is mandatory and occurs once. Its fields map directly to
`BaseStructure`:

| Statement | Model field | Sparse default |
| --- | --- | --- |
| `source_locale` | `source_locale` | required |
| `target_locale` | `target_locale` | absent means `None` |
| `target_locales` | `target_locales` | absent means `()` |
| `format_version` | `format_version` | absent means `"0.1"` |
| `export_origin` | `export_origin` | absent means `""` |
| `export_timestamp` | `export_timestamp` | absent means `""` |
| `source_language` | `source_language` | absent means `None` |
| `target_language` | `target_language` | absent means `None` |
| `target_languages` | `target_languages` | absent means `()` |
| `extension "k" = "v"` | `extensions[k]` | repeated ordered map entry |

Units follow as top-level `unit "id"` blocks. Unit IDs must be unique. Their
order is significant and is preserved.

## Translation units

The unit body maps to `Data`:

```lokit
unit "cart.items" {
  source = "{count} item"
  target = "{count} article"
  plural {
    variant = "{count} items"
    count = 0
    category = other
  }
  meta {
    usage_count = 12
    max_length = 80
  }
  status = reviewed
  comment {
    context = "Checkout summary"
    origin {
      system = "cms"
      project = "storefront"
    }
  }
  previous_context {
    unit_id = "cart.heading"
  }
  extension "resource" = "checkout"
}
```

`source` is required. Optional blocks are `plural`, `tags`, `previous_context`,
and `next_context`. `meta` may be omitted for the always-present default
`Meta()`. Comments are ordered repeated blocks. Extensions are ordered repeated
statements. The unknown status is the default and may be omitted.

Multilingual targets are ordered blocks keyed by locale:

```lokit
unit "greeting" {
  source = "Hello"
  target "fr-FR" {
    text = "Bonjour"
    status = approved
  }
  target "de-DE" {
    text = "Hallo"
  }
}
```

A target block with no `text` represents an existing `TargetData` whose text is
`None`. `text = ""` represents an existing empty translation. In canonical
order, target blocks contain `text`, `status`, `tags`, `plural`, `meta`,
repeated `comment`, and repeated `extension` entries, omitting defaults by the
same sparse rules as unit data.

## Plurals, metadata, comments, and context

A `plural` block requires `variant`; it optionally contains `count`, `category`,
and extensions. Categories are `generic`, `zero`, `one`, `two`, `few`, `many`,
and `other`.

A `meta` block may contain `usage_count`, `last_used`, `first_used`, `created`,
`updated`, `max_length`, `min_length`, and extensions.

A `comment` block requires `context`; its remaining canonical order is
`timestamp`, `origin`, `context_key`, and extensions. `origin` uses `system`,
`project`, `creator_id`, and extensions in that order.

`previous_context` and `next_context` blocks may contain `unit_id`, `source`,
`target`, and extensions. Empty optional blocks are valid and preserve object
presence.

## Inline content

Unit-level `tags` preserves both source and legacy single-target inline data:

```lokit
tags {
  source_tag "open" {
    id = "b1"
    type = strong.open
    pair_id = "pair-1"
    original_name = "strong"
  }
  source_tag "close" {
    id = "b2"
    type = strong.close
    pair_id = "pair-1"
    original_name = "strong"
  }
  source_parts {
    code = "open"
    text = "Important"
    code = "close"
  }
}
```

The available blocks are `source_tag`, `target_tag`, `source_parts`, and
`target_parts`. A target-specific `tags` block instead contains repeated `tag`
blocks and one `parts` block.

A tag block maps to `TieData`. It requires `id` and `type`. Repeated
`attribute "name" = "value"` statements follow `type`, followed by
`attribute_data`, `position`, `order`, `pair_id`, `original_name`, and
`original_text`. The quoted block label is the map key and is intentionally
independent of `TieData.id`.

Parts preserve order through repeated `text` and `code` statements. A `code`
value refers to the containing tag map's key. Dangling references do not make
the file syntactically undecodable because the Python model can contain them,
but validators and the language server report them.

## Canonical statement order

This section is normative for schema version 1. A parser accepts recognized
statements in any unambiguous order, but a canonical writer MUST emit each
block in the sequence below. Missing sparse/default values are skipped without
changing the relative order of fields that remain. Repeated collections and
ordered maps retain model order and are never sorted.

| Context | Canonical body sequence |
| --- | --- |
| `document` | `source_locale`; `target_locale`; `target_locales`; `format_version`; `export_origin`; `export_timestamp`; `source_language`; `target_language`; `target_languages`; every `extension` entry |
| `unit` / `Data` | `source`; legacy `target`; every keyed `target "locale"` block; `plural`; unit-level `tags`; `meta`; `status`; every `comment`; `previous_context`; `next_context`; every `extension` entry |
| keyed `target` / `TargetData` | `text`; `status`; target-specific `tags`; `plural`; `meta`; every `comment`; every `extension` entry |
| `plural` | `variant`; `count`; `category`; every `extension` entry |
| unit-level `tags` | every `source_tag`; every `target_tag`; `source_parts`; `target_parts` |
| target-specific `tags` | every `tag`; `parts` |
| `meta` | `usage_count`; `last_used`; `first_used`; `created`; `updated`; `max_length`; `min_length`; every `extension` entry |
| `comment` | `context`; `timestamp`; `origin`; `context_key`; every `extension` entry |
| `origin` | `system`; `project`; `creator_id`; every `extension` entry |
| `previous_context` or `next_context` | `unit_id`; `source`; `target`; every `extension` entry |
| `source_tag`, `target_tag`, or `tag` / `TieData` | `id`; `type`; every `attribute` entry; `attribute_data`; `position`; `order`; `pair_id`; `original_name`; `original_text` |
| `source_parts`, `target_parts`, or `parts` | one `text` or `code` statement for each `SegmentPart`, in exact list order |

Extensions are therefore always the final statements in every block that owns
an extension map. Attributes are the only analogous keyed statements within a
tag block and occur as one contiguous group immediately after `type`. Empty
optional blocks still emit their opening and closing lines at the position
shown above.

## Grammar sketch

This EBNF describes the lexical shape. The allowed properties and child blocks
are determined by the typed context described above.

```text
document       = magic, newline, document-block, { top-level }, eof ;
magic          = "@lokit 1" ;
document-block = "document", ws, "{", newline, body, "}" ;
top-level      = blank | comment | unit-block ;
unit-block     = "unit", ws, string, ws, "{", newline, body, "}" ;
body           = { blank | comment | property | keyed-property | block } ;
property       = identifier, ws, "=", ws, value, newline ;
keyed-property = ("extension" | "attribute"), ws, string,
                 ws, "=", ws, string, newline ;
block          = identifier, [ ws, string ], ws, "{", newline, body, "}" ;
value          = string | integer | identifier | string-list ;
string-list    = "[", [ string, { ",", ws, string } ], "]" ;
comment        = comment-prefix, "#", { unicode-scalar }, newline ;
comment-prefix = { " " | tab } ;
blank          = indentation, newline ;
indentation    = { "  " } ;
```

Properties and single-valued blocks may not be duplicated. Map keys and unit
IDs may not be duplicated. Unknown fields, blocks, enum values, or a different
schema major version are errors. Diagnostics include a stable error code and a
one-based line and Unicode-scalar column. Invalid UTF-8 is located after the
valid scalar prefix preceding the bad byte. An oversized line is located at
the greatest valid scalar boundary at or before the byte limit, never in the
middle of a multibyte encoding.

## Streaming and limits

The document block is parsed first. Each following unit is then parsed and
returned independently; a reader need not materialize prior or future units.
Writers similarly emit metadata followed by units from an iterable.

The reference parser and canonical writer limit a physical line to 1 MiB
(1,048,576 bytes). Canonical measurement excludes the terminating LF; for
noncanonical CRLF input the CR is part of the physical line presented to the
parser. The parser limits structural nesting to 16 levels. They fail with a
located or typed error rather than panicking on malformed, truncated,
oversized, invalid UTF-8, lone-surrogate, or out-of-range input. String escapes
are decoded only after syntax validation.
Duplicate detection and validation state may grow with
the number of unique unit IDs, target locales, extensions, and tag keys, but
unit payload memory is released after each streamed record.

## Canonicalization and compatibility

Canonical writers use the normative order in
[Canonical statement order](#canonical-statement-order), preserve model order
for all repeated values, and emit exactly the inter-block blank lines defined
in [Encoding](#encoding). They omit only values whose constructor default
reconstructs the same model value. They never add or rewrite
`extensions["input_format"]`.

Schema-major changes may alter grammar or model meaning. A version-1 reader
rejects unsupported major versions. Backward-compatible vendor or source-format
data belongs in existing extension maps, which remain string-to-string maps and
round-trip without interpretation.

## Media type

The file extension is `.lokit`. Until a media type is registered, applications
should use `text/x-lokit; charset=utf-8`.

## Design references

- [TOON specification](https://toonformat.dev/reference/spec)
- [Language Server Protocol specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/)
