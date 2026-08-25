using System.IO.Compression;
using System.Xml.Linq;
using Lokit.Office.Core.Packaging;

namespace Lokit.Office.Core.Extraction;

public sealed class OfficeExtractor
{
    private static readonly XNamespace Word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    private static readonly XNamespace Drawing = "http://schemas.openxmlformats.org/drawingml/2006/main";
    private static readonly HashSet<string> MetadataProperties = new(StringComparer.Ordinal)
    {
        "category",
        "contentStatus",
        "coverage",
        "creator",
        "description",
        "identifier",
        "keywords",
        "language",
        "publisher",
        "relation",
        "rights",
        "source",
        "subject",
        "title",
        "type",
    };

    public ExtractionResult Extract(string sourcePath, string expectedFormat, OfficeOptions options)
    {
        var result = ExtractStreaming(sourcePath, expectedFormat, options);
        return new ExtractionResult(
            result.Format,
            result.SourceFingerprint,
            result.Units.ToList(),
            result.Warnings);
    }

    public StreamingExtractionResult ExtractStreaming(string sourcePath, string expectedFormat, OfficeOptions options)
    {
        var package = PackagePreflight.Inspect(sourcePath, options);
        using var archive = ZipFile.OpenRead(sourcePath);
        var actualFormat = OoxmlPackage.DetectFormat(archive, package.Names, options);
        if (actualFormat != expectedFormat)
        {
            throw new OfficeUnsupportedPackageException($"Expected {expectedFormat.ToUpperInvariant()} package, detected {actualFormat.ToUpperInvariant()}");
        }

        var parts = expectedFormat == "docx"
            ? OoxmlPackage.DocxParts(package.Names, options)
            : OoxmlPackage.PptxParts(archive, package.Names, options);
        return new StreamingExtractionResult(
            expectedFormat,
            package.Fingerprint,
            ExtractParts(sourcePath, expectedFormat, package, parts, options),
            Array.Empty<OfficeWarning>());
    }

    private static IEnumerable<OfficeUnit> ExtractParts(
        string sourcePath,
        string expectedFormat,
        PackageInfo package,
        IReadOnlyList<string> parts,
        OfficeOptions options)
    {
        using var archive = ZipFile.OpenRead(sourcePath);
        foreach (var part in parts)
        {
            if (!package.Names.Contains(part))
            {
                continue;
            }
            var xml = OoxmlPackage.ReadXml(archive, part, options.MaxUnitBytes);
            if (expectedFormat == "docx")
            {
                foreach (var unit in ExtractDocxPart(xml, part, package.Fingerprint, options))
                {
                    yield return unit;
                }
            }
            else
            {
                foreach (var unit in ExtractPptxPart(xml, part, package.Fingerprint, options))
                {
                    yield return unit;
                }
            }
        }
    }

    private static IEnumerable<OfficeUnit> ExtractDocxPart(XDocument xml, string part, string fingerprint, OfficeOptions options)
    {
        var container = DocxContainer(part);
        var index = 0;
        foreach (var paragraph in xml.Descendants(Word + "p"))
        {
            var text = DocxParagraphText(paragraph);
            if (!string.IsNullOrWhiteSpace(text))
            {
                if (TextLimits.ExceedsUnicodeScalarLimit(text, options.MaxTextUnitChars))
                {
                    throw new OfficePackageException("DOCX text unit exceeds max_text_unit_chars");
                }
                yield return new OfficeUnit(
                    $"docx:{container}:p/{index}",
                    text,
                    part,
                    Extensions("docx", part, container, fingerprint));
            }
            index += 1;
        }
    }

    private static IEnumerable<OfficeUnit> ExtractPptxPart(XDocument xml, string part, string fingerprint, OfficeOptions options)
    {
        if (!options.IncludeHiddenSlides && IsHiddenSlide(xml, part))
        {
            yield break;
        }
        var container = PptxContainer(part);
        if (PptxAreaEnabled(part, options))
        {
            var areaUnits = IsMetadataPart(part)
                ? ExtractMetadata(xml, part, container, fingerprint, options)
                : IsCommentPart(part)
                    ? ExtractComments(xml, part, container, fingerprint, options)
                    : ExtractPptxParagraphs(xml, part, container, fingerprint, options);
            foreach (var unit in areaUnits)
            {
                yield return unit;
            }
        }
        if (options.IncludeAltText)
        {
            foreach (var unit in ExtractAltText(xml, part, container, fingerprint, options))
            {
                yield return unit;
            }
        }
    }

    private static IEnumerable<OfficeUnit> ExtractPptxParagraphs(
        XDocument xml,
        string part,
        string container,
        string fingerprint,
        OfficeOptions options)
    {
        var index = 0;
        foreach (var paragraph in xml.Descendants(Drawing + "p"))
        {
            var text = PptxParagraphText(paragraph);
            if (!string.IsNullOrEmpty(text) && (!string.IsNullOrWhiteSpace(text) || PptxArea(part) == "diagrams"))
            {
                ValidatePptxText(text, options);
                var extensions = Extensions("pptx", part, container, fingerprint);
                extensions["office.area"] = PptxArea(part);
                if (part.StartsWith("ppt/slides/slide", StringComparison.Ordinal) ||
                    part.StartsWith("ppt/notesSlides/notesSlide", StringComparison.Ordinal))
                {
                    var slideNumber = OoxmlPackage.SlideNumber(part);
                    if (slideNumber > 0)
                    {
                        extensions["office.slide_number"] = slideNumber.ToString(
                            System.Globalization.CultureInfo.InvariantCulture);
                    }
                }
                yield return new OfficeUnit(
                    $"pptx:{container}:p/{index}",
                    text,
                    part,
                    extensions);
            }
            index += 1;
        }
    }

    private static IEnumerable<OfficeUnit> ExtractComments(
        XDocument xml,
        string part,
        string container,
        string fingerprint,
        OfficeOptions options)
    {
        var index = 0;
        foreach (var element in xml.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
        {
            if (element.Name.LocalName == "text" && !string.IsNullOrWhiteSpace(element.Value))
            {
                ValidatePptxText(element.Value, options);
                var extensions = Extensions("pptx", part, container, fingerprint);
                extensions["office.area"] = "comments";
                extensions["office.node_kind"] = "comment";
                yield return new OfficeUnit(
                    $"pptx:{container}:comment/{index}",
                    element.Value,
                    part,
                    extensions);
            }
            index += 1;
        }
    }

    private static IEnumerable<OfficeUnit> ExtractMetadata(
        XDocument xml,
        string part,
        string container,
        string fingerprint,
        OfficeOptions options)
    {
        var index = 0;
        foreach (var element in xml.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
        {
            var propertyName = MetadataProperty(element, part);
            if (propertyName is not null && !string.IsNullOrWhiteSpace(element.Value))
            {
                ValidatePptxText(element.Value, options);
                var extensions = Extensions("pptx", part, container, fingerprint);
                extensions["office.area"] = "document_metadata";
                extensions["office.node_kind"] = "metadata";
                extensions["office.property"] = propertyName;
                yield return new OfficeUnit(
                    $"pptx:{container}:property/{propertyName}/{index}",
                    element.Value,
                    part,
                    extensions);
            }
            index += 1;
        }
    }

    private static IEnumerable<OfficeUnit> ExtractAltText(
        XDocument xml,
        string part,
        string container,
        string fingerprint,
        OfficeOptions options)
    {
        var index = 0;
        foreach (var element in xml.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
        {
            var attributes = new List<string> { "title", "descr" };
            foreach (var attributeName in attributes)
            {
                var text = (string?)element.Attribute(attributeName) ?? string.Empty;
                if (string.IsNullOrWhiteSpace(text))
                {
                    continue;
                }
                ValidatePptxText(text, options);
                var extensions = Extensions("pptx", part, container, fingerprint);
                extensions["office.area"] = "alt_text";
                extensions["office.alt_text"] = "true";
                extensions["office.attribute"] = attributeName;
                yield return new OfficeUnit(
                    $"pptx:{container}:alt/{index}/{attributeName}",
                    text,
                    part,
                    extensions);
            }
            index += 1;
        }
    }

    private static void ValidatePptxText(string text, OfficeOptions options)
    {
        if (TextLimits.ExceedsUnicodeScalarLimit(text, options.MaxTextUnitChars))
        {
            throw new OfficePackageException("PPTX text unit exceeds max_text_unit_chars");
        }
    }

    private static string DocxParagraphText(XElement paragraph)
    {
        var parts = new List<string>();
        foreach (var element in paragraph.Descendants())
        {
            if (element.Name == Word + "t")
            {
                parts.Add(element.Value);
            }
            else if (element.Name == Word + "tab")
            {
                parts.Add("\t");
            }
            else if (element.Name == Word + "br" || element.Name == Word + "cr")
            {
                parts.Add("\n");
            }
        }
        return string.Concat(parts);
    }

    private static string PptxParagraphText(XElement paragraph)
    {
        var parts = new List<string>();
        foreach (var element in paragraph.Descendants())
        {
            if (element.Ancestors(Drawing + "fld").Any())
            {
                continue;
            }
            if (element.Name == Drawing + "t")
            {
                parts.Add(element.Value);
            }
            else if (element.Name == Drawing + "br")
            {
                parts.Add("\n");
            }
        }
        return string.Concat(parts);
    }

    private static Dictionary<string, string> Extensions(string format, string part, string container, string fingerprint)
    {
        return new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["input_format"] = format,
            ["office.format"] = format,
            ["office.part"] = part,
            ["office.container"] = container,
            ["office.source_fingerprint"] = fingerprint,
        };
    }

    public static string DocxContainer(string part)
    {
        if (part == "word/document.xml")
        {
            return "body";
        }
        if (part.StartsWith("word/header", StringComparison.Ordinal))
        {
            return $"header/{Path.GetFileNameWithoutExtension(part).Replace("header", "", StringComparison.Ordinal)}";
        }
        if (part.StartsWith("word/footer", StringComparison.Ordinal))
        {
            return $"footer/{Path.GetFileNameWithoutExtension(part).Replace("footer", "", StringComparison.Ordinal)}";
        }
        if (part == "word/comments.xml")
        {
            return "comment";
        }
        return Path.GetFileNameWithoutExtension(part);
    }

    public static string PptxContainer(string part)
    {
        if (part.StartsWith("ppt/slides/slide", StringComparison.Ordinal))
        {
            return $"slide/{OoxmlPackage.SlideNumber(part)}";
        }
        if (part.StartsWith("ppt/notesSlides/notesSlide", StringComparison.Ordinal))
        {
            return $"slide/{OoxmlPackage.SlideNumber(part)}:notes";
        }
        if (part.StartsWith("ppt/slideLayouts/", StringComparison.Ordinal))
        {
            return $"layout/{Path.GetFileNameWithoutExtension(part)}";
        }
        if (part.StartsWith("ppt/slideMasters/", StringComparison.Ordinal))
        {
            return $"master/{Path.GetFileNameWithoutExtension(part)}";
        }
        if (part.StartsWith("ppt/notesMasters/", StringComparison.Ordinal))
        {
            return $"notes-master/{Path.GetFileNameWithoutExtension(part)}";
        }
        if (part.StartsWith("ppt/handoutMasters/", StringComparison.Ordinal))
        {
            return $"handout-master/{Path.GetFileNameWithoutExtension(part)}";
        }
        if (part.StartsWith("ppt/comments/", StringComparison.Ordinal))
        {
            return $"comment/{Path.GetFileNameWithoutExtension(part)}";
        }
        if (part.StartsWith("ppt/charts/", StringComparison.Ordinal))
        {
            return $"chart/{Path.GetFileNameWithoutExtension(part)}";
        }
        if (part.StartsWith("ppt/diagrams/", StringComparison.Ordinal))
        {
            return $"diagram/{Path.GetFileNameWithoutExtension(part)}";
        }
        if (part.StartsWith("docProps/", StringComparison.Ordinal))
        {
            return $"metadata/{Path.GetFileNameWithoutExtension(part)}";
        }
        return Path.GetFileNameWithoutExtension(part);
    }

    private static bool PptxAreaEnabled(string part, OfficeOptions options)
    {
        return PptxArea(part) switch
        {
            "slides" => options.IncludeSlides,
            "speaker_notes" => options.IncludeSpeakerNotes && options.IncludeNotes,
            "slide_masters" => options.IncludeSlideMasters && options.IncludeMasterLayoutContent,
            "slide_layouts" => options.IncludeSlideLayouts && options.IncludeMasterLayoutContent,
            "notes_masters" => options.IncludeNotesMasters,
            "handout_masters" => options.IncludeHandoutMasters,
            "comments" => options.IncludeComments,
            "charts" => options.IncludeCharts,
            "diagrams" => options.IncludeDiagrams,
            "document_metadata" => options.IncludeDocumentMetadata,
            _ => false,
        };
    }

    private static string PptxArea(string part)
    {
        if (part.StartsWith("ppt/slides/slide", StringComparison.Ordinal))
        {
            return "slides";
        }
        if (part.StartsWith("ppt/notesSlides/notesSlide", StringComparison.Ordinal))
        {
            return "speaker_notes";
        }
        if (part.StartsWith("ppt/slideMasters/slideMaster", StringComparison.Ordinal))
        {
            return "slide_masters";
        }
        if (part.StartsWith("ppt/slideLayouts/slideLayout", StringComparison.Ordinal))
        {
            return "slide_layouts";
        }
        if (part.StartsWith("ppt/notesMasters/notesMaster", StringComparison.Ordinal))
        {
            return "notes_masters";
        }
        if (part.StartsWith("ppt/handoutMasters/handoutMaster", StringComparison.Ordinal))
        {
            return "handout_masters";
        }
        if (IsCommentPart(part))
        {
            return "comments";
        }
        if (part.StartsWith("ppt/charts/chart", StringComparison.Ordinal))
        {
            return "charts";
        }
        if (part.StartsWith("ppt/diagrams/data", StringComparison.Ordinal))
        {
            return "diagrams";
        }
        return IsMetadataPart(part) ? "document_metadata" : string.Empty;
    }

    private static bool IsCommentPart(string part)
    {
        return part.StartsWith("ppt/comments/comment", StringComparison.Ordinal);
    }

    private static bool IsMetadataPart(string part)
    {
        return part.StartsWith("docProps/core", StringComparison.Ordinal) ||
            part.StartsWith("docProps/custom", StringComparison.Ordinal);
    }

    private static string? MetadataProperty(XElement element, string part)
    {
        if (part.StartsWith("docProps/core", StringComparison.Ordinal))
        {
            return MetadataProperties.Contains(element.Name.LocalName) ? element.Name.LocalName : null;
        }
        if (element.Parent?.Name.LocalName == "property" && !element.HasElements)
        {
            return (string?)element.Parent.Attribute("name") ?? element.Name.LocalName;
        }
        return null;
    }

    private static bool IsHiddenSlide(XDocument xml, string part)
    {
        if (!part.StartsWith("ppt/slides/slide", StringComparison.Ordinal))
        {
            return false;
        }
        var value = ((string?)xml.Root?.Attribute("show") ?? string.Empty).Trim();
        return value.Equals("0", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("false", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("off", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("no", StringComparison.OrdinalIgnoreCase);
    }
}
