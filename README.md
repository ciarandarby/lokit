# Lokit

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/lokit-python?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=BLUE&left_text=downloads)](https://pepy.tech/projects/lokit-python)

**[`lokit-python` on PyPI](https://pypi.org/project/lokit-python/)**

The most modern and high performance localization framework out there

[**Documentation:** www.lokit.org](https://www.lokit.org)

[Contributing](https://github.com/ciarandarby/lokit/blob/HEAD/.github/CONTRIBUTING.md) · [Report a bug or request a feature](https://github.com/ciarandarby/lokit/issues/new/choose)

## TL;DR

Lokit is a high performance localization package for Python with its backend written in Rust and C#. Lokit parses, sanitizes and extracts data to and from localization interchanges like `.tmx` and `.xliff` at record speeds and memory usage.

It does this by streaming files with its Rust backend, peak RSS much is lower than any localization tool out there. Other packages and legacy systems ingest the whole XML tree into memory. It works off a unified `BaseStructure`, the same base structure for all file formats. This also equates to how performative Lokit is on speed.

Lokit also supports other major file format parsing for extraction, translation, and target file generation. It allows you to use the original source to produce a localized copy.

Lokit is a full-feature toolkit with all batteries included from file parsing, to inline tag handling to database ingestion and fuzzy matching.

**Install via pip:**

```bash
pip install lokit-python
```

**Install via uv:**

```bash
uv add lokit-python
```

**Or build locally:**

```bash
uv pip install lokit-python --no-binary :all:
```

## What is Lokit?

Lokit is a cutting edge localization tool that is used for parsing localization interchange formats, common file formats and full backend pipeline tools to cover all areas. Lokit solves major localization engineering headaches such as inline tag parsing and managing. It's the fastest and most memory safe localization toolkit out there, in any programming ecosystem.

## How it's different to other packages

Lokit is built fundamentally different to legacy toolkits like Okapi (Java) and Translate Toolkit (Python). It uses data streaming instead of loading entire datasets into memory, localization interchange files are usually very large. It's also written in Rust for maximum performance and C# for native Office file type parsing and handling.

## How it works

All file types, both localization interchange and consumer file types are streamed into a common, unified `BaseStructure`. This `BaseStructure` is equipped with adapters for all supported file types. There are two main options when parsing with Lokit:

### Materialization

**`lokit.parse`**

This is where the entire file streams to memory. Its main purpose is for handling the data in your code.

### Direct Streaming

**`lokit.stream`**

This is used for directly streaming one file type to another. When direct streaming is used, nothing stays in memory. This means you can stream a 10 GB `.tmx` file with a peak RSS of less than 100 MB at blazing fast speeds.

Lokit isn't just for file parsing also packed with a full toolkit for backend localization engineering with everything needed for a high-performance backend tech stack.

## Features

- Parses all major file formats both consumer and localization
- **Regeneration**: this uses a base model that is extracted from a source file, then translated and uses the `regen` method to reference the source file to generate the translated target.
- Inline tag and placeholder handling. This is a major headache in localization engineering, here's how Lokit makes this easy:
  - **Full inline tag handling**: inline and runtime tags are completely handled by Lokit. It replaces the tags with `{Lokit_1}` (Customizable) and stores their metadata in a map. Lokit then handles the tags again when parsing back into a file format. This is configurable, and can be turned off.
  - **Intelligent Placeholder Mapping**: this works in the same way to how Lokit handles tags but with inline placeholders, resolving them during export. This is also configurable and can be disabled.
- **Database**: Lokit has APIs to stream to and from Postgres databases to build project and translation memory data. It supports local Postgres and major services like GCP, AWS, Supabase and Neon.
- **TM Matching**: Lokit has APIs for matching source translations to targets with custom thresholds and attributes from files, memory and databases.

## Performance

Benchmarks show that Lokit is by far the highest performance localization toolkit for both raw speed, and memory usage. Enterprise level TMX files take seconds to parse with unseen peak memory usage. This is significant for many reasons; localization systems are notoriously slow, heavy, and archaic. With the cost of compute rising, performance is more necessary than ever.

Here are some numbers when parsing a large TMX file (500k segments) to XLIFF in comparison to other localization tools:

| Library | Total Time | Units/s | Speed | Peak RSS (MiB) | RSS Difference |
| :--- | ---: | ---: | ---: | ---: | ---: |
| **Lokit** | 4.112 s | 121,609 | baseline | 42.8 MiB | baseline |
| **Okapi Framework 1.48.0** | 4.220 s | 118,480 | -2.6% | 647.2 MiB | +1,412.1% |
| **translate-toolkit 3.19.13** | 15.210 s | 32,872 | -269.9% | 1,833.0 MiB | +4,182.7% |

These benchmarks have not yet been conducted by a third party so please run these to verify.

## Lokit is easy to use

Lokit has been designed to purposely be very easy to use with no boilerplate. Most actions can be done in a single line of code.

For example, when parsing and exporting between file formats:

```python
import lokit

# One liner conversion from TMX to XLIFF
lokit.stream.tmx("path/to/source.tmx").export.xliff("path/to/export.xliff")

# Regeneration from source file
doc = lokit.parse.docx("path/to/source.docx")

# Translation

doc.regen.docx("path/to/source.docx")

# Save translations as TMX:
doc.export.tmx("path/to/tm.tmx")
```

- Lokit is strictly typed and mypy strict compatible.
- All of Lokit's type annotations can also be imported for use with stubs attached by default.
- Language codes are automatically detected or can be inserted into args.
- APIs are available in synchronous and asynchronous formats.

<!--
Search Tags & Keywords for SEO:
python localization toolkit, python translation memory database, tmx parser python, xliff parser python, gettext po parser, localization backend as a service, postgresql translation memory, pg_trgm fuzzy matching, python i18n l10n tools, translate-toolkit alternative, localization interchange format converter, async streaming xml parser, type-safe localization, mypyc compiled python, localization, parsing, localization database, portable object, translation memory, translation management system, i18n, l10n, lokit, lokit-python
-->
