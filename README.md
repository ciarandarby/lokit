# lokit

> [!WARNING]
> **Beta Release:** lokit is currently in Beta. The API is volatile and subject to rapid, breaking changes prior to the official V1 release.

<br>

lokit is a high-performance, strictly type-safe, and highly memory-efficient localization toolkit for Python.

<br>

Supports Python 3.12+.

<br>
<br>
<hr>
<br>
<br>

Unlike legacy tools that wrap around XML DOM element trees in-memory, lokit represents a shift away from XML-based localization interchange formats towards native language parsing. It ingests localization formats (TMX, XLIFF, PO, XLSX, CSV, JSON, HTML, IDML) and compiles them into a strict, unified structural data model. This enables not just parsing, but robust data manipulation, semantic extraction, and advanced translation memory features out-of-the-box. Lokit focuses on streaming and asynchronous processing rather than synchronous events using in-memory files.

<br>

This format type can be easily converted to JSON for interchange with other systems. I've made parsing and data transfers as native as possible by capturing all elements of traditional interchange formats in a common format structure. This allows for much better compatibility, especially in terms of segment matching and leveraging as it uses flattened strings as standard. Tags are preserved but as a common format, meaning the structure parsed from XLIFF will be the same as the structure parsed from HTML.

<br>

These legacy file formats have supported vendor-lock in for many year, making it difficult for any client to move to another system. Seeing that this is a major issue across the domain, something new is needed where vendors do not use hidden, legacy technology to lock in their clients. Localization deserves innovation.

<br>
<br>
<hr>
<br>
<br>

> The main premise here is a common, structured and type-safe dataclass model structure that is intentionally compatible with any file format, not just localization interchange formats, although these are optimized for performance and memory efficiency due to the verbose nature of XML based formats.

<br>

Note: This project was originally written in Rust and is still unreleased. Adding Rust extensions did not show a major performance improvement over the current C-Extension modules due to bridging overheads, this will be re-addressed in future releases. SDKs in other languages including the Rust prototype are coming soon.

<br>
<br>
<hr>
<br>
<br>

## Core Features

<br>

lokit provides a comprehensive suite of tools for managing localization data:

* **Native Structural Modeling:** Converts interchange formats into a strict, unified Python Data classes, ensuring complete type safety.
* **Advanced Matching Engine:** Provides Exact Matching, Fuzzy Matching (via SequenceMatcher), and In-Context Exact (ICE) Matching leveraging previous and next segment context, as well as with inline tags.
* **Sub-segment Extraction:** Automatically parses and isolates inline tags, properties, and formatting markers, allowing for safe manipulation of text without corrupting code.
* **Semantic Querying:** Easily filter translation units using any attribute, exact ID lookups, or deep nested JSON path querying (`where()`).
* **Plural Support:** Native extraction and structuring of pluralized translation units, compatible with UI frameworks.
* **Universal Format Conversion:** Instantly import and export between any supported format (e.g., TMX to JSON, HTML to XLIFF) with zero data loss.
* **Synchronous and Asynchronous Streaming:** Process massive enterprise files natively using Python async generators to keep memory overhead to an absolute minimum.

<br>

### Type Safety and C-Extensions

<br>

The entire library is very strictly typed and mypy compliant, so strict it compiles to C-extensions via mypyc and pre-attached via wheels. Additionally, any XML processing uses C-based packages. Compiling to these extensions has shown a 23% in overall performance increases over pure-python modules with additional benefits such as lower memory usage. C extensions are standard for MacOS (ARM+Intel), Windows, and Linux.

<br>
<br>

## Parsing Performance

<br>

When dealing with enterprise-scale localization environments, parsing performance and memory efficiency are paramount. lokit is designed to be significantly leaner and faster than the industry standard.

<br>

Using another package, `translate-toolkit`, as a reference as it is the de-facto and feature-rich standard for localization file format parsing and conversion in Python for comparison, we benchmarked lokit's modules against its equivalents.

<br>

In a stress-test benchmark on a +600 MB `.TMX` file containing **557,058 segments**, converting to JSON with `Lokit.to_json_async()` over 3 iterations yielded the following comparative averages:

<br>

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
<br>
<hr>
<br>
<br>

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

Converting files synchronously is straightforward using the modular importers and exporters APIs. The packages are designed to be as simple and easy to work with to write no boilerplate code while still being memory effecient.

```python
from lokit.importers import import_tmx
from lokit.exporters import export_xliff

# Parse the interchange file into the common datatypes
document = import_tmx("path/to/source.tmx")

# Export the BaseStructure to whatever file type:
export_xliff(document, "path/to/target.xliff")
```

<br>

### Asynchronous Streaming for Large Interchange Files

<br>

For files spanning hundreds of megabytes, parsing the entire DOM structure into memory is inefficient. Lokit supports stream-parsing natively.

<br>

Here's some simple scripting code to show how easy it is. This simple program has no boilderplate and can be reduced to a few lines of code, but for the purpose of showcasing, we added some wrapper functions. The stream APIs take the static attributes such as language codes, keeping them in an immutable state. Then quickly streams the mutables. All other parsing modules also use streaming to parse to and from the common typed format.

```python
import asyncio
import os

from lokit import Lokit

input_dir = "data/language_tmx"
output_dir = "data/out"


async def convert_to_json(filepath: str):
    print(f"Starting: {filepath}")
    streamer = Lokit.to_json_async
    output = f"{output_dir}/{os.path.splitext(os.path.basename(filepath))[0]}.json"
    await streamer(
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
from lokit.logic import Lokit

# Wrap a parsed document or path in the Lokit logic engine
engine = Lokit.parse("path/to/source.xliff")

# Query specific nested data structures
button_units = engine.where("extensions.component", "checkout_button")

# Perform fuzzy matching against translation memory
results = engine.fuzzy_find("Complete your purchase", limit=5, threshold=0.75)
for match in results:
    print(f"Match found: {match.unit_id} (Score: {match.score})")

# Perform strict In-Context Exact (ICE) matching
ice_match = engine.match(
    source="Submit",
    target_unit_id="submit_btn_1",
    previous_source="Enter your email",
    require_context=True
)
```

<br>
<br>
<hr>
<br>
<br>

## Supported Formats

<br>

* TMX (Translation Memory eXchange)
* XLIFF (XML Localization Interchange File Format)
* PO/POT (Gettext Portable Object)
* XLSX / CSV (Spreadsheets)
* JSON (Key-Value nested localization trees) [Also supports direct stream parsing]
* HTML (Hypertext Markup)
* IDML (InDesign Markup Language)
