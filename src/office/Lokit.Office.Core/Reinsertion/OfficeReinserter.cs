using System.IO.Compression;
using System.Xml.Linq;
using Lokit.Office.Core.Extraction;
using Lokit.Office.Core.Packaging;

namespace Lokit.Office.Core.Reinsertion;

public sealed class OfficeReinserter
{
    private static readonly XNamespace Word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    private static readonly XNamespace Drawing = "http://schemas.openxmlformats.org/drawingml/2006/main";
    private static readonly XNamespace Xml = "http://www.w3.org/XML/1998/namespace";
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

    public ReinsertionResult Reinsert(
        string sourcePath,
        string outputPath,
        string expectedFormat,
        IReadOnlyDictionary<string, string> translations,
        OfficeOptions options)
    {
        var package = PackagePreflight.Inspect(sourcePath, options);
        using var input = ZipFile.OpenRead(sourcePath);
        var actualFormat = OoxmlPackage.DetectFormat(input, package.Names, options);
        if (actualFormat != expectedFormat)
        {
            throw new OfficeUnsupportedPackageException($"Expected {expectedFormat.ToUpperInvariant()} package, detected {actualFormat.ToUpperInvariant()}");
        }

        var rewriteParts = expectedFormat == "docx"
            ? OoxmlPackage.DocxParts(package.Names, options).ToHashSet(StringComparer.Ordinal)
            : OoxmlPackage.PptxParts(input, package.Names, options).ToHashSet(StringComparer.Ordinal);
        var consumed = new HashSet<string>(StringComparer.Ordinal);
        var warnings = new List<OfficeWarning>();

        if (File.Exists(outputPath))
        {
            File.Delete(outputPath);
        }
        using (var output = ZipFile.Open(outputPath, ZipArchiveMode.Create))
        {
            foreach (var entry in input.Entries)
            {
                var outputEntry = output.CreateEntry(entry.FullName, CompressionLevel.Optimal);
                CopyAttributes(entry, outputEntry);
                using var outputStream = outputEntry.Open();
                if (rewriteParts.Contains(entry.FullName))
                {
                    var document = OoxmlPackage.ReadXml(input, entry.FullName, options.MaxUnitBytes);
                    var consumedBefore = consumed.Count;
                    RewritePart(document, entry.FullName, expectedFormat, translations, consumed, options, warnings);
                    if (consumed.Count == consumedBefore)
                    {
                        using var inputStream = entry.Open();
                        inputStream.CopyTo(outputStream);
                    }
                    else
                    {
                        document.Save(outputStream, SaveOptions.DisableFormatting);
                    }
                }
                else
                {
                    using var inputStream = entry.Open();
                    inputStream.CopyTo(outputStream);
                }
            }
        }

        var extras = translations.Keys.Where(key => !consumed.Contains(key)).ToArray();
        if (extras.Length > 0)
        {
            var message = $"{extras.Length} supplied translation unit(s) did not match source document";
            if (options.ExtraTranslationPolicy == "error")
            {
                throw new OfficeReinsertionException(message);
            }
            warnings.Add(new OfficeWarning("office.extra_translation", message));
        }

        return new ReinsertionResult(
            consumed.Count,
            warnings,
            package.Fingerprint,
            new FileInfo(outputPath).Length);
    }

    private static void RewritePart(
        XDocument document,
        string part,
        string format,
        IReadOnlyDictionary<string, string> translations,
        HashSet<string> consumed,
        OfficeOptions options,
        List<OfficeWarning> warnings)
    {
        if (format == "pptx")
        {
            RewritePptxPart(document, part, translations, consumed, options, warnings);
            return;
        }
        var container = OfficeExtractor.DocxContainer(part);
        var index = 0;
        foreach (var paragraph in document.Descendants(Word + "p"))
        {
            var unitId = $"{format}:{container}:p/{index}";
            if (translations.TryGetValue(unitId, out var translation))
            {
                ReplaceDocxParagraph(paragraph, translation);
                consumed.Add(unitId);
            }
            else
            {
                HandleMissingTranslation(unitId, DocxParagraphText(paragraph), part, options, warnings);
            }
            index += 1;
        }
    }

    private static void RewritePptxPart(
        XDocument document,
        string part,
        IReadOnlyDictionary<string, string> translations,
        HashSet<string> consumed,
        OfficeOptions options,
        List<OfficeWarning> warnings)
    {
        if (!options.IncludeHiddenSlides && IsHiddenSlide(document, part))
        {
            return;
        }
        var container = OfficeExtractor.PptxContainer(part);
        if (PptxAreaEnabled(part, options))
        {
            if (IsMetadataPart(part))
            {
                RewriteMetadata(document, part, container, translations, consumed, options, warnings);
            }
            else if (IsCommentPart(part))
            {
                RewriteComments(document, part, container, translations, consumed, options, warnings);
            }
            else
            {
                var index = 0;
                foreach (var paragraph in document.Descendants(Drawing + "p"))
                {
                    var unitId = $"pptx:{container}:p/{index}";
                    if (translations.TryGetValue(unitId, out var translation))
                    {
                        ReplacePptxParagraph(paragraph, translation);
                        consumed.Add(unitId);
                    }
                    else
                    {
                        HandleMissingTranslation(
                            unitId,
                            PptxParagraphText(paragraph),
                            part,
                            options,
                            warnings,
                            preserveWhitespace: PptxArea(part) == "diagrams");
                    }
                    index += 1;
                }
            }
        }
        if (options.IncludeAltText)
        {
            RewriteAltText(document, part, container, translations, consumed, options, warnings);
        }
    }

    private static void RewriteComments(
        XDocument document,
        string part,
        string container,
        IReadOnlyDictionary<string, string> translations,
        HashSet<string> consumed,
        OfficeOptions options,
        List<OfficeWarning> warnings)
    {
        var index = 0;
        foreach (var element in document.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
        {
            if (element.Name.LocalName == "text")
            {
                var unitId = $"pptx:{container}:comment/{index}";
                if (translations.TryGetValue(unitId, out var translation))
                {
                    element.Value = translation;
                    consumed.Add(unitId);
                }
                else
                {
                    HandleMissingTranslation(unitId, element.Value, part, options, warnings);
                }
            }
            index += 1;
        }
    }

    private static void RewriteMetadata(
        XDocument document,
        string part,
        string container,
        IReadOnlyDictionary<string, string> translations,
        HashSet<string> consumed,
        OfficeOptions options,
        List<OfficeWarning> warnings)
    {
        var index = 0;
        foreach (var element in document.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
        {
            var propertyName = MetadataProperty(element, part);
            if (propertyName is not null)
            {
                var unitId = $"pptx:{container}:property/{propertyName}/{index}";
                if (translations.TryGetValue(unitId, out var translation))
                {
                    element.Value = translation;
                    consumed.Add(unitId);
                }
                else
                {
                    HandleMissingTranslation(unitId, element.Value, part, options, warnings);
                }
            }
            index += 1;
        }
    }

    private static void RewriteAltText(
        XDocument document,
        string part,
        string container,
        IReadOnlyDictionary<string, string> translations,
        HashSet<string> consumed,
        OfficeOptions options,
        List<OfficeWarning> warnings)
    {
        var index = 0;
        foreach (var element in document.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
        {
            var attributes = new List<string> { "title", "descr" };
            foreach (var attributeName in attributes)
            {
                var source = (string?)element.Attribute(attributeName) ?? string.Empty;
                if (string.IsNullOrWhiteSpace(source))
                {
                    continue;
                }
                var unitId = $"pptx:{container}:alt/{index}/{attributeName}";
                if (translations.TryGetValue(unitId, out var translation))
                {
                    element.SetAttributeValue(attributeName, translation);
                    consumed.Add(unitId);
                }
                else
                {
                    HandleMissingTranslation(unitId, source, part, options, warnings);
                }
            }
            index += 1;
        }
    }

    private static void HandleMissingTranslation(
        string unitId,
        string source,
        string part,
        OfficeOptions options,
        List<OfficeWarning> warnings,
        bool preserveWhitespace = false)
    {
        if (string.IsNullOrEmpty(source) || (!preserveWhitespace && string.IsNullOrWhiteSpace(source)))
        {
            return;
        }
        if (options.MissingTranslationPolicy == "error")
        {
            throw new OfficeReinsertionException($"Missing translation for {unitId}");
        }
        if (options.MissingTranslationPolicy == "warn")
        {
            warnings.Add(new OfficeWarning("office.missing_translation", $"Missing translation for {unitId}", unitId, part));
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

    private static void ReplaceDocxParagraph(XElement paragraph, string text)
    {
        var nodes = paragraph.Descendants(Word + "t").ToList();
        if (nodes.Count == 0)
        {
            var run = new XElement(Word + "r");
            var textNode = new XElement(Word + "t", text);
            textNode.SetAttributeValue(Xml + "space", "preserve");
            run.Add(textNode);
            paragraph.Add(run);
            return;
        }
        nodes[0].Value = text;
        nodes[0].SetAttributeValue(Xml + "space", "preserve");
        foreach (var node in nodes.Skip(1))
        {
            node.Value = string.Empty;
        }
    }

    private static void ReplacePptxParagraph(XElement paragraph, string text)
    {
        var content = paragraph.Elements()
            .Where(element => element.Name == Drawing + "r" || element.Name == Drawing + "br")
            .ToList();
        var templateProperties = content
            .FirstOrDefault(element => element.Name == Drawing + "r")?
            .Element(Drawing + "rPr");
        var replacement = new List<XElement>();
        var lines = text.Split('\n');
        for (var index = 0; index < lines.Length; index += 1)
        {
            if (index > 0)
            {
                replacement.Add(new XElement(Drawing + "br"));
            }
            var textNode = new XElement(Drawing + "t", lines[index]);
            textNode.SetAttributeValue(Xml + "space", "preserve");
            var run = new XElement(Drawing + "r");
            if (templateProperties is not null)
            {
                run.Add(new XElement(templateProperties));
            }
            run.Add(textNode);
            replacement.Add(run);
        }

        var insertionPoint = content.FirstOrDefault() ?? paragraph.Element(Drawing + "endParaRPr");
        if (insertionPoint is null)
        {
            paragraph.Add(replacement);
        }
        else
        {
            insertionPoint.AddBeforeSelf(replacement);
        }
        foreach (var element in content)
        {
            element.Remove();
        }
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

    private static bool IsHiddenSlide(XDocument document, string part)
    {
        if (!part.StartsWith("ppt/slides/slide", StringComparison.Ordinal))
        {
            return false;
        }
        var value = ((string?)document.Root?.Attribute("show") ?? string.Empty).Trim();
        return value.Equals("0", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("false", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("off", StringComparison.OrdinalIgnoreCase) ||
            value.Equals("no", StringComparison.OrdinalIgnoreCase);
    }

    private static void CopyAttributes(ZipArchiveEntry source, ZipArchiveEntry target)
    {
        target.LastWriteTime = source.LastWriteTime;
        target.ExternalAttributes = source.ExternalAttributes;
    }
}
