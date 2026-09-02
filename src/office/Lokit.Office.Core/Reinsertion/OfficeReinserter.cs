using System.IO.Compression;
using System.Text;
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
        ValidateTranslations(translations, options);
        var resolvedOutputPath = Path.GetFullPath(outputPath);
        var outputDirectory = Path.GetDirectoryName(resolvedOutputPath) ?? Directory.GetCurrentDirectory();
        Directory.CreateDirectory(outputDirectory);
        var temporaryPath = Path.Combine(
            outputDirectory,
            $".{Path.GetFileName(resolvedOutputPath)}.{Guid.NewGuid():N}.tmp");
        try
        {
            var result = ReinsertToTemporary(
                sourcePath,
                temporaryPath,
                expectedFormat,
                translations,
                options);
            var outputBytes = new FileInfo(temporaryPath).Length;
            File.Move(temporaryPath, resolvedOutputPath, overwrite: true);
            return result with { OutputBytes = outputBytes };
        }
        catch
        {
            TryDelete(temporaryPath);
            throw;
        }
    }

    private static ReinsertionResult ReinsertToTemporary(
        string sourcePath,
        string temporaryPath,
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

        using (var output = ZipFile.Open(temporaryPath, ZipArchiveMode.Create))
        {
            foreach (var entry in input.Entries)
            {
                var outputEntry = output.CreateEntry(entry.FullName, CompressionLevel.Optimal);
                CopyAttributes(entry, outputEntry);
                using var outputStream = outputEntry.Open();
                if (rewriteParts.Contains(entry.FullName))
                {
                    var document = OoxmlPackage.ReadXml(input, entry.FullName, options.MaxUnitBytes);
                    PreflightRewritePart(document, entry.FullName, expectedFormat, translations, options);
                    var consumedBefore = consumed.Count;
                    RewritePart(document, entry.FullName, expectedFormat, translations, consumed, options, warnings);
                    if (consumed.Count == consumedBefore)
                    {
                        using var inputStream = entry.Open();
                        inputStream.CopyTo(outputStream);
                    }
                    else
                    {
                        SaveBounded(document, outputStream, entry.FullName, options.MaxUnitBytes);
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
            0);
    }

    private static void ValidateTranslations(
        IReadOnlyDictionary<string, string> translations,
        OfficeOptions options)
    {
        if (options.MaxTextUnitChars < 1)
        {
            throw new OfficeReinsertionException("max_text_unit_chars must be at least 1");
        }
        foreach (var target in translations.Values)
        {
            if (TextLimits.ExceedsUnicodeScalarLimit(target, options.MaxTextUnitChars))
            {
                throw new OfficeReinsertionException("Office translation exceeds max_text_unit_chars");
            }
        }
    }

    private static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (IOException)
        {
            // Preserve the original failure; the temporary file is never the requested output.
        }
        catch (UnauthorizedAccessException)
        {
            // Preserve the original failure; the temporary file is never the requested output.
        }
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

    private static void PreflightRewritePart(
        XDocument document,
        string part,
        string format,
        IReadOnlyDictionary<string, string> translations,
        OfficeOptions options)
    {
        using var growth = RewriteGrowth(document, part, format, translations, options).GetEnumerator();
        if (!growth.MoveNext())
        {
            return;
        }

        var estimated = SerializedSize(document, options.MaxUnitBytes, part);
        do
        {
            var delta = growth.Current;
            if (delta > options.MaxUnitBytes - estimated)
            {
                throw RewrittenPartTooLarge(part);
            }
            estimated += delta;
        }
        while (growth.MoveNext());
    }

    private static IEnumerable<long> RewriteGrowth(
        XDocument document,
        string part,
        string format,
        IReadOnlyDictionary<string, string> translations,
        OfficeOptions options)
    {
        if (format == "docx")
        {
            foreach (var delta in DocxRewriteGrowth(document, part, translations))
            {
                yield return delta;
            }
            yield break;
        }

        foreach (var delta in PptxRewriteGrowth(document, part, translations, options))
        {
            yield return delta;
        }
    }

    private static IEnumerable<long> DocxRewriteGrowth(
        XDocument document,
        string part,
        IReadOnlyDictionary<string, string> translations)
    {
        var container = OfficeExtractor.DocxContainer(part);
        var index = 0;
        foreach (var paragraph in document.Descendants(Word + "p"))
        {
            var unitId = $"docx:{container}:p/{index}";
            if (translations.TryGetValue(unitId, out var replacement))
            {
                var textNodes = paragraph.Descendants(Word + "t").ToList();
                long removed = 0;
                foreach (var node in textNodes)
                {
                    removed = AddSaturated(removed, Utf8ByteCount(node.Value));
                }
                var structure = textNodes.Count == 0 ? 256L : 64L;
                yield return PositiveGrowth(EscapedUpperByteCount(replacement), structure, removed);
            }
            index += 1;
        }
    }

    private static IEnumerable<long> PptxRewriteGrowth(
        XDocument document,
        string part,
        IReadOnlyDictionary<string, string> translations,
        OfficeOptions options)
    {
        if (!options.IncludeHiddenSlides && IsHiddenSlide(document, part))
        {
            yield break;
        }

        var container = OfficeExtractor.PptxContainer(part);
        if (PptxAreaEnabled(part, options))
        {
            if (IsMetadataPart(part))
            {
                var index = 0;
                foreach (var element in document.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
                {
                    var propertyName = MetadataProperty(element, part);
                    if (propertyName is not null && translations.TryGetValue(
                        $"pptx:{container}:property/{propertyName}/{index}",
                        out var replacement))
                    {
                        yield return PositiveGrowth(
                            EscapedUpperByteCount(replacement),
                            0,
                            Utf8ByteCount(element.Value));
                    }
                    index += 1;
                }
            }
            else if (IsCommentPart(part))
            {
                var index = 0;
                foreach (var element in document.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
                {
                    if (element.Name.LocalName == "text" && translations.TryGetValue(
                        $"pptx:{container}:comment/{index}",
                        out var replacement))
                    {
                        yield return PositiveGrowth(
                            EscapedUpperByteCount(replacement),
                            0,
                            Utf8ByteCount(element.Value));
                    }
                    index += 1;
                }
            }
            else
            {
                var index = 0;
                foreach (var paragraph in document.Descendants(Drawing + "p"))
                {
                    if (translations.TryGetValue($"pptx:{container}:p/{index}", out var replacement))
                    {
                        var content = paragraph.Elements()
                            .Where(element => element.Name == Drawing + "r" || element.Name == Drawing + "br")
                            .ToList();
                        var templateProperties = content
                            .FirstOrDefault(element => element.Name == Drawing + "r")?
                            .Element(Drawing + "rPr");
                        var propertiesBytes = templateProperties is null
                            ? 0
                            : SerializedSize(templateProperties);
                        var lines = 1L + replacement.LongCount(character => character == '\n');
                        var structure = AddSaturated(
                            MultiplySaturated(lines, AddSaturated(192, propertiesBytes)),
                            MultiplySaturated(lines - 1, 64));
                        long removed = 0;
                        foreach (var element in content)
                        {
                            foreach (var textNode in element.Descendants(Drawing + "t"))
                            {
                                removed = AddSaturated(removed, Utf8ByteCount(textNode.Value));
                            }
                        }
                        yield return PositiveGrowth(
                            EscapedUpperByteCount(replacement),
                            structure,
                            removed);
                    }
                    index += 1;
                }
            }
        }

        if (options.IncludeAltText)
        {
            var index = 0;
            foreach (var element in document.Root?.DescendantsAndSelf() ?? Enumerable.Empty<XElement>())
            {
                foreach (var attributeName in new[] { "title", "descr" })
                {
                    var source = (string?)element.Attribute(attributeName) ?? string.Empty;
                    if (!string.IsNullOrWhiteSpace(source) && translations.TryGetValue(
                        $"pptx:{container}:alt/{index}/{attributeName}",
                        out var replacement))
                    {
                        yield return PositiveGrowth(
                            EscapedUpperByteCount(replacement, attribute: true),
                            0,
                            Utf8ByteCount(source));
                    }
                }
                index += 1;
            }
        }
    }

    private static long SerializedSize(XDocument document, long limit, string part)
    {
        using var counter = new BoundedWriteStream(null, limit, part);
        try
        {
            document.Save(counter, SaveOptions.DisableFormatting);
            return counter.BytesWritten;
        }
        catch (OfficeReinsertionException)
        {
            throw;
        }
        catch (Exception exc) when (exc is System.Xml.XmlException or InvalidOperationException or ArgumentException)
        {
            throw new OfficeReinsertionException($"Unable to serialize Office XML part: {part}", exc);
        }
    }

    private static long SerializedSize(XElement element)
    {
        using var counter = new BoundedWriteStream(null, long.MaxValue, "run properties");
        element.Save(counter, SaveOptions.DisableFormatting);
        return counter.BytesWritten;
    }

    private static void SaveBounded(XDocument document, Stream output, string part, long limit)
    {
        using var bounded = new BoundedWriteStream(output, limit, part, leaveOpen: true);
        try
        {
            document.Save(bounded, SaveOptions.DisableFormatting);
            bounded.Flush();
        }
        catch (OfficeReinsertionException)
        {
            throw;
        }
        catch (Exception exc) when (exc is System.Xml.XmlException or InvalidOperationException or ArgumentException)
        {
            throw new OfficeReinsertionException($"Unable to serialize Office XML part: {part}", exc);
        }
    }

    private static OfficeReinsertionException RewrittenPartTooLarge(string part)
    {
        return new OfficeReinsertionException($"Office rewritten XML part exceeds max_unit_bytes: {part}");
    }

    private static long EscapedUpperByteCount(string text, bool attribute = false)
    {
        long total = 0;
        foreach (var rune in text.EnumerateRunes())
        {
            var bytes = rune.Value switch
            {
                '&' => 5,
                '<' or '>' => 4,
                '\r' => 5,
                '"' when attribute => 6,
                '\t' or '\n' when attribute => 5,
                _ => rune.Utf8SequenceLength,
            };
            total = AddSaturated(total, bytes);
        }
        return total;
    }

    private static long Utf8ByteCount(string text)
    {
        return Encoding.UTF8.GetByteCount(text);
    }

    private static long PositiveGrowth(long replacement, long structure, long removed)
    {
        var added = AddSaturated(replacement, structure);
        return added > removed ? added - removed : 0;
    }

    private static long AddSaturated(long left, long right)
    {
        return right > long.MaxValue - left ? long.MaxValue : left + right;
    }

    private static long MultiplySaturated(long left, long right)
    {
        if (left == 0 || right == 0)
        {
            return 0;
        }
        return left > long.MaxValue / right ? long.MaxValue : left * right;
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

    private sealed class BoundedWriteStream : Stream
    {
        private readonly Stream? _inner;
        private readonly long _limit;
        private readonly string _part;
        private readonly bool _leaveOpen;

        public BoundedWriteStream(Stream? inner, long limit, string part, bool leaveOpen = false)
        {
            _inner = inner;
            _limit = limit;
            _part = part;
            _leaveOpen = leaveOpen;
        }

        public long BytesWritten { get; private set; }
        public override bool CanRead => false;
        public override bool CanSeek => false;
        public override bool CanWrite => true;
        public override long Length => BytesWritten;
        public override long Position
        {
            get => BytesWritten;
            set => throw new NotSupportedException();
        }

        public override void Flush()
        {
            _inner?.Flush();
        }

        public override void Write(byte[] buffer, int offset, int count)
        {
            Write(buffer.AsSpan(offset, count));
        }

        public override void Write(ReadOnlySpan<byte> buffer)
        {
            if (buffer.Length > _limit - BytesWritten)
            {
                throw RewrittenPartTooLarge(_part);
            }
            _inner?.Write(buffer);
            BytesWritten += buffer.Length;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && !_leaveOpen)
            {
                _inner?.Dispose();
            }
            base.Dispose(disposing);
        }

        public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
    }
}
