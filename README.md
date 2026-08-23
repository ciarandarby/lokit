# Lokit

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/lokit-python?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=BLUE&left_text=downloads)](https://pepy.tech/projects/lokit-python)

`The fastest and highest performing localization library.`

> [!WARNING]
> **Beta Release:** lokit is currently in Beta. The API is volatile and subject to rapid, breaking changes prior to the official V1 release.

<br>

Supports Python 3.10+.

<hr>

Unlike legacy tools that wrap XML DOM element trees in memory, Lokit ingests localization formats (`.lokit`, TMX, XLIFF, PO, XLSX, CSV, JSON, HTML, IDML, DOCX, and PPTX) into one strict structural data model. This enables parsing, robust data manipulation, semantic extraction, and translation-memory features without coupling applications to a source format. Lokit emphasizes bounded streaming and asynchronous processing for large files.

<br>

This format type can be easily converted to JSON for interchange with other systems. I've made parsing and data transfers as native as possible by capturing all elements of traditional interchange formats in a common format structure. This allows for much better compatibility, especially in terms of segment matching and leveraging as it uses flattened strings as standard. Tags are preserved but as a common format, meaning the structure parsed from XLIFF will be the same as the structure parsed from HTML.

<br>

These legacy file formats have supported vendor-lock in for many year, making it difficult for any client to move to another system. Seeing that this is a major issue across the domain, something new is needed where vendors do not use hidden, legacy technology to lock in their clients. Localization deserves innovation. Lokit is the first open source package that supports direct localization interchange to database ingestion; even outside the Python ecosystem.

<br>
<hr>

> The main premise here is a common, structured and type-safe dataclass model structure that is intentionally compatible with any file format, not just localization interchange formats, although these are optimized for performance and memory efficiency due to the verbose nature of XML based formats.

<br>

The TMX, XLIFF, and `.lokit` paths require the native Rust parser included in every wheel. They do not fall back to a second Python file parser. The Python projection layer remains strictly typed and is compiled with mypyc in release wheels.

<br>

## Core Features

<br>

Lokit provides a comprehensive suite of tools for managing localization data:

* **Native Structural Modeling:** Converts interchange formats into a strict, unified Python Data classes, ensuring complete type safety.
* **Advanced Matching Engine:** Provides Exact Matching, Fuzzy Matching (via SequenceMatcher), and In-Context Exact (ICE) Matching leveraging previous and next segment context, as well as with inline tags.
* **Sub-segment Extraction:** Automatically parses and isolates inline tags, properties, and formatting markers, allowing for safe manipulation of text without corrupting code.
* **Semantic Querying:** Easily filter translation units using any attribute, exact ID lookups, or deep nested JSON path querying (`where()`).
* **Plural Support:** Native extraction and structuring of pluralized translation units, compatible with UI frameworks.
* **Universal Format Conversion:** Instantly import and export between any supported format (e.g., TMX to JSON, HTML to XLIFF) with zero data loss.
* **Sparse Native Interchange:** Round-trip every Lokit model field within explicit v1 safety bounds through the versioned, no-`null`, streamable `.lokit` format.
* **Synchronous and Asynchronous Streaming:** Process massive enterprise files natively using Python async generators to keep memory overhead to an absolute minimum.
* **Native DOCX & PPTX Support:** Using C# extensions without external dependencies ensuring no overhead and no data loss.

<br>

### Type Safety and C-Extensions

<br>

The entire library is very strictly typed and mypy compliant, so strict it compiles to C-extensions via mypyc and pre-attached via wheels. Additionally, any XML processing uses C-based packages. Compiling to these extensions has shown a 23% in overall performance increases over pure-python modules with additional benefits such as lower memory usage. C extensions are standard for MacOS (ARM+Intel), Windows, and Linux.

<br>

## Parsing Performance

<br>

When dealing with enterprise-scale localization environments, parsing performance and memory efficiency are paramount. Lokit is designed to be significantly leaner and faster than the industry standard. Current benchmarks show much higher performance than any other localziation library in any other language for parsing localization files.

<br>

**Benchmarks**

To demonstrate converting common file types between localization interchange files, the following shows performance metrics against comparable tools in other programming languages. This parsing stress test used the sequence DOCX -> CSV -> XLIFF -> TMX -> CSV -> XLIFF -> DOCX with a monolingual source.

| Language | Library | Total Time (s) | Peak Memory (MB) |
| :--- | :--- | :--- | :--- |
| Python | Lokit | 4.29 | 393.39 |
| Rust | quick-xml + csv | 23.11 | 26.66 |
| Go | encoding/xml + encoding/csv | 23.63 | 61.88 |
| Node.js | sax | 29.9 | 36.11 |
| Java | Okapi Framework | 463.15 | 952.68 |


<br>
Using another package, `translate-toolkit`, as a reference as it is the de-facto and feature-rich standard for localization file format parsing and conversion in Python for comparison, we benchmarked lokit's modules against its equivalents. In a stress-test benchmark on a +600 MB `.TMX` file containing over **550,000 segments**, converting to normalized JSON file over 3 iterations yielded the following comparative averages:


| Library | Avg Duration | Peak Memory | Memory Efficiency |
|---------|------------------|------------------|-------------------|
| **lokit** | 13.57s | 135.9 MB | 15x Less Memory |
| **translate-toolkit** | 20.30s | 2,034.5 MB | ~2.0 GB |

<br>

Tests for both covered from TMX to JSON with inline tag sanitization in both using the respective packages' tooling.

<br>

The major focus on memory safety allows for parallel processing of events, making it suitable for large-scale localization workflows and backend systems.

<br>

**Note:** this package is not a replacement or substitution for the already amazing translate-toolkit. The functionality is quite differet across both libraries and have their own use cases.

<br>
<hr>

## SDK Usage Reference

<br>

Lokit operates around a central `BaseStructure` dataclass model, which standardizes localization units and segments. This instructs better standardization and branching in a more language native way compared to XML based file formats. Parsing SDKs are added for both extraction and export tasks for localization interchange formats along with common file types.

<br>

### Installation

<br>

Install lokit via pip:

```bash
pip install lokit-python
```

<br>

### Basic Parsing and Conversion

<br>

Converting files synchronously is straightforward through the modular `lokit` API. Import the package once, then use `lokit.parse` and `lokit.parse.write`.

```python
import lokit

document = lokit.parse.tmx("path/to/source.tmx")
document = lokit.parse.lokit("path/to/catalog.lokit")
document = lokit.parse.docx("path/to/document.docx")
document = lokit.parse.pptx("path/to/presentation.pptx")
documents = lokit.parse.files(["memory.tmx", "catalog.xliff", "messages.po"])

lokit.parse.write.xliff(document, "path/to/target.xliff")
lokit.export.lokit(document, "path/to/catalog.lokit")
document.export.csv("path/to/target.csv")
```

PO parsing defaults to standard Gettext semantics, where `msgid` is the source and `msgstr` is the translation. Catalogs that use `msgid` as a stable key can select the translated text as the source explicitly; `.pot` files are detected as source templates:

```python
catalog = lokit.stream.po("path/to/fr.po", mode="msgid_as_id")
template = lokit.parse.po("path/to/messages.pot", mode="auto", progress=False)
```

The equivalent explicit standard mode is `mode="msgid_as_source"`. Both modes are available on parse, stream, and async PO entry points.
The native streaming reader accepts up to 1 MiB of content per physical PO line and 16 MiB of cumulative raw input per logical entry, returning a located parse error when either safety limit is exceeded.

PowerPoint extraction includes slides, speaker notes, slide masters, used slide layouts, notes and handout masters, comments, charts, SmartArt/diagrams, document metadata, alt text, and hidden slides by default. Each area can be disabled independently for parsing, streaming, and export:

```python
from lokit.office import OfficeExportOptions, OfficeImportOptions

parse_options = OfficeImportOptions(
    include_speaker_notes=False,
    include_comments=False,
    include_document_metadata=False,
)
presentation = lokit.parse.pptx(
    "path/to/presentation.pptx",
    options=parse_options,
)

presentation.export.pptx(
    "path/to/translated.pptx",
    options=OfficeExportOptions(include_speaker_notes=False),
)
```

### Native `.lokit` interchange

`.lokit` is Lokit's lossless interchange format for the documented `BaseStructure` domain. It uses a compact, line-oriented syntax inspired by TOON's readability, but it is a distinct localization schema. Missing optional values are omitted instead of encoded as `null`; present empty strings, zeroes, and empty optional objects remain distinguishable and round-trip exactly. Version 1 represents signed 64-bit integers and limits canonical physical lines to 1 MiB; out-of-domain values fail explicitly and never replace an existing output.

```lokit
@lokit 1
document {
  source_locale = "en-US"
  target_locale = "fr-FR"
}
unit "home.title" {
  source = "Welcome"
  target = "Bienvenue"
  status = translated
}
```

All three API styles are available in synchronous and asynchronous form:

```python
import lokit

document = lokit.parse.lokit("messages.lokit")
stream = lokit.stream.lokit("messages.lokit")
lokit.export.lokit(document, "copy.lokit")


async def copy_catalog() -> None:
    units = [unit async for unit in lokit.parse.async_.lokit("messages.lokit")]
    await lokit.export.async_.lokit(document, "async-copy.lokit")
    assert units
```

Full consumption closes async readers automatically. For an intentional early
exit, use the returned bounded bridge as an async context manager so its reader
is closed immediately:

```python
async with lokit.stream.async_.lokit("messages.lokit") as units:
    async for unit_id, data in units:
        print(unit_id, data.source)
        break
```

The complete grammar, field mapping, canonicalization rules, and compatibility policy are in [`docs/lokit-format.md`](docs/lokit-format.md); implementation decisions are in [`docs/lokit-architecture.md`](docs/lokit-architecture.md), and reproducible measurements are in [`docs/lokit-performance.md`](docs/lokit-performance.md). The standalone Rust language server and editor setup are documented in [`tools/lokit-lsp/README.md`](tools/lokit-lsp/README.md).

### Dictionary projections

Interchange dictionary rows are separate from JSON-i18n documents and from
newline-delimited JSON output. `lokit.stream.to_dict` yields rows lazily;
`lokit.parse.to_dict` materializes the same rows. The default schema is
`source_language`, `target_language`, `source`, `target`, and `domain`, and a
multilingual unit yields one row per target locale in document order.

```python
import lokit
from lokit.types import DictField, StringMode

rows = lokit.parse.to_dict("messages.tmx")

for row in lokit.stream.to_dict(
    "messages.xliff",
    target_language="fr",
    strings=StringMode.RAW,
    fields=(
        DictField.UNIT_ID,
        DictField.SOURCE_LOCALE,
        DictField.TARGET_LOCALE,
        DictField.SOURCE,
        DictField.TARGET,
        DictField.DOMAIN,
    ),
):
    print(row)
```

`StringMode.SANITIZED` returns plain text. `StringMode.RAW` reconstructs the
source-format inline XML, including original tag names, attributes, and inline
payloads. The asynchronous equivalents are `lokit.stream.async_.to_dict` and
`lokit.parse.async_.to_dict`. Existing JSON-i18n parsing remains available as
`lokit.parse.json_i18n`; use `lokit.stream.write_jsonl` when a JSONL file is the
desired output.

### Splitting multilingual documents

Materialized documents split into independent single-target models. A
one-shot streaming document uses a context manager backed by bounded native
`.lokit` spools, so it never duplicates the source iterator or retains the
whole import in memory.

```python
from pathlib import Path

import lokit

document = lokit.parse.tmx("multilingual.tmx", progress=False)
for locale, localized in document.split_targets().items():
    localized.export.xliff(Path("out") / f"messages-{locale}.xliff")

stream = lokit.stream.xliff("multilingual.xliff")
with stream.split_targets(include_missing=False) as localized_streams:
    for locale, localized in localized_streams.items():
        localized.export.lokit(Path("out") / f"messages-{locale}.lokit")
```

The streaming split files are valid only inside the context. Pass an explicit
tuple of target locales to either `split_targets` method to select a subset.

<br>

### Asynchronous Streaming for Large Interchange Files

<br>

For files spanning hundreds of megabytes, parsing the entire DOM structure into memory is inefficient. Lokit supports stream-parsing natively.

<br>

Here is a complete scripting example. It can be reduced to a few lines, but the wrapper functions make each stage explicit. The stream APIs keep document-level attributes such as language codes immutable while yielding translation units incrementally. The other parsers use the same common typed model.

```python
import asyncio
import os

import lokit

input_dir = "data/language_tmx"
output_dir = "data/out"


async def convert_to_json(filepath: str):
    print(f"Starting: {filepath}")
    output = f"{output_dir}/{os.path.splitext(os.path.basename(filepath))[0]}.json"
    await lokit.stream.async_.write_jsonl(
        filepath=filepath,
        output=output,
    )
    print(f"Completed: {output}")


async def process():
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    files = [os.path.join(input_dir, i) for i in os.listdir(input_dir)]
    tasks = [convert_to_json(filepath=file) for file in files]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(process())
```

<br>

### Advanced Querying and Matching

<br>

The `Lokit` logic wrapper provides access to the powerful matching engine and data manipulation features. This does not substitute for enterprise database semantic search but can be used as an after-step for evaluating matching results after retrieving translation units from a semantic/vector database.

```python
import lokit

engine = lokit.Lokit.parse("path/to/source.xliff")

button_units = engine.where("extensions.component", "checkout_button")

results = engine.fuzzy_find("Complete your purchase", limit=5, threshold=0.75)
for match in results:
    print(f"Match found: {match.unit_id} (Score: {match.score})")

ice_match = engine.match(
    source="Submit",
    target_unit_id="submit_btn_1",
    previous_source="Enter your email",
    require_context=True
)
```

### Structured API Paths

The preferred public API is available from a single package import:

```python
import lokit

document = lokit.parse.file("path/to/source.tmx")
documents = lokit.parse.files(["path/to/source.tmx", "path/to/source.xliff"])
document = lokit.parse.lokit("path/to/source.lokit")
document = lokit.parse.csv("path/to/source.csv", source_locale="en-US")
document = lokit.parse.docx("path/to/source.docx")
streamed_tmx = lokit.stream.tmx("path/to/source.tmx")
streamed_lokit = lokit.stream.lokit("path/to/source.lokit")
streamed_docx = lokit.stream.docx("path/to/source.docx")


async def stream_to_json() -> None:
    await lokit.stream.async_.write_jsonl("path/to/source.tmx", "path/to/out.jsonl")

lokit.parse.write.csv(document, "path/to/target.csv")
lokit.export.lokit(document, "path/to/target.lokit")
document.export.xliff("path/to/target.xliff")
document.export.docx("path/to/translated.docx", source_docx="path/to/source.docx")


async def export_xlsx() -> None:
    await lokit.parse.write.async_.xlsx(document, "path/to/target.xlsx")

CsvExtractor = lokit.parsers.extractors.csv
```

Predefined conversions live under `lokit.quick_parse` for fast one-liner conversions:

```python
import lokit

stats = lokit.quick_parse.tmx_to_csv("path/to/source.tmx", "path/to/target.csv")
lokit.quick_parse.csv_to_xliff("path/to/source.csv", "path/to/target.xliff")
```

<br>

### PostgreSQL API

Lokit is the first localization package to have native support for storing translations in local and enterprise databases. Fast and effeciant TM matching is also included out of the box.

Here's an example of how easy it is to ingest and use the TM database:

```python
import lokit


async def load_and_match() -> None:
    tm = await lokit.database.connect("postgresql://localhost/lokit_tm")
    async with tm:
        await tm.setup()

        stream = lokit.stream.tmx("translation_memory.tmx")
        await tm.load(stream)

        results = await tm.match(
            source="Roses are red",
            source_locale="en-US",
            target_locale="fr-FR",
            limit=5,
            threshold=0.5,
        )
        print(results[0].unit_id, results[0].kind, results[0].score)
```

The database stores plain source and target text in PostgreSQL, uses `pg_trgm`
for exact and fuzzy lookup, and reconstructs Lokit `Data` objects with tags,
comments, metadata, and adjacent context. This supports plain-string matching
with tag and metadata propagation.

The stable row serializers and ordered schema statements are public for custom
SQL loaders and migration tools such as Alembic:

```python
from alembic import op

from lokit.database import database_schema_statements

for statement in database_schema_statements(partitioned=True):
    op.execute(statement)
```

```python
import lokit
from lokit.database import iter_serialized_units

document = lokit.stream.tmx("translation_memory.tmx")
for serialized in iter_serialized_units(document, project="checkout", domain="web"):
    unit_row = serialized.unit
    tag_rows = serialized.tags
```

`lokit.database.serialization` also exports the typed insert/fetch row models,
`serialize_unit`, and `deserialize_unit` for integrations that own their SQL
execution and retrieval lifecycle.

<br>

### Enterprise Database Support

With the above local database ingestion and runtime logic, Lokit has direct connection APIs to external enterprise services.
Currently supporting AWS (RDS & Aurora), GCP (Cloud SQL & AlloyDB) along with serverless platforms Supabase and Neon.
Pipeline is support and enabled by default but configurable.
Dual read and write URIs are also accepted for maximum performance while a single URI can still be used for simplicity or where it is not supported in the service used.

The API includes a full backend framework for handling localization database operations including matching, tag, pluralization and properity propigation, read and writes, and concurrent data handeling to and from the database server. All in async and with concurrency where supported by the service. 

Lokit can handle direct streaming from legacy interchange formats to enterprise databses with complete customization, no hidden dependencies, no boilerplate and highly optimized data flows.

Lokit is the first ever package to support this in any language ecosystem.

```python
import lokit

tm_rds = await lokit.database.connect(
    "postgresql://user:pass@instance.rds.amazonaws.com:5432/tm?sslmode=require"
)

tm_aurora = await lokit.database.connect(
    "postgresql://user:pass@cluster.rds.amazonaws.com:5432/tm?sslmode=require",
    reader_uri="postgresql://user:pass@cluster-ro.rds.amazonaws.com:5432/tm?sslmode=require"
)

tm_gcp = await lokit.database.connect(
    "postgresql://user:pass@/tm?host=/cloudsql/project:region:instance"
)

tm_supabase = await lokit.database.connect(
    "postgresql://postgres.project-ref:pass@aws-0-region.pooler.supabase.com:6543/postgres?sslmode=require",
    pipeline=False
)

tm_neon = await lokit.database.connect(
    "postgresql://user:pass@ep-cool-darkness-123456.us-east-2.aws.neon.tech/tm?sslmode=require",
    pipeline=False
)
```

<br>
<hr>


## Supported Formats for Parsing

<br>

* TMX
* XLIFF 
* PO/POT
* XLSX
* CSV
* JSON
* HTML
* IDML
* DOCX
* PPTX

<br>
<hr>

## Learn More

Visit the official homepage at **[lokit.org](https://lokit.org)**, more detailed documentation is to come before the V1 release.

<!-- 
Search Tags & Keywords for SEO:
python localization toolkit, python translation memory database, tmx parser python, xliff parser python, gettext po parser, localization backend as a service, postgresql translation memory, pg_trgm fuzzy matching, python i18n l10n tools, translate-toolkit alternative, localization interchange format converter, async streaming xml parser, type-safe localization, mypyc compiled python, localizaiton, parsing, localization database, portable object, translation memory, translation management system, i18n, l10n, lokit, lokit-python
-->
