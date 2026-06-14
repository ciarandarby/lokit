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

    public ReinsertionResult Reinsert(
        string sourcePath,
        string outputPath,
        string expectedFormat,
        IReadOnlyDictionary<string, string> translations,
        OfficeOptions options)
    {
        var package = PackagePreflight.Inspect(sourcePath, options);
        using var input = ZipFile.OpenRead(sourcePath);
        var actualFormat = OoxmlPackage.DetectFormat(input, package.Names);
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
                    var document = OoxmlPackage.ReadXml(input, entry.FullName);
                    RewritePart(document, entry.FullName, expectedFormat, translations, consumed, options, warnings);
                    document.Save(outputStream, SaveOptions.DisableFormatting);
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
        var paragraphName = format == "docx" ? Word + "p" : Drawing + "p";
        var container = format == "docx" ? OfficeExtractor.DocxContainer(part) : OfficeExtractor.PptxContainer(part);
        var index = 0;
        foreach (var paragraph in document.Descendants(paragraphName))
        {
            var unitId = $"{format}:{container}:p/{index}";
            if (translations.TryGetValue(unitId, out var translation))
            {
                if (format == "docx")
                {
                    ReplaceDocxParagraph(paragraph, translation);
                }
                else
                {
                    ReplacePptxParagraph(paragraph, translation);
                }
                consumed.Add(unitId);
            }
            else if (options.MissingTranslationPolicy == "error")
            {
                throw new OfficeReinsertionException($"Missing translation for {unitId}");
            }
            else if (options.MissingTranslationPolicy == "warn")
            {
                warnings.Add(new OfficeWarning("office.missing_translation", $"Missing translation for {unitId}", unitId, part));
            }
            index += 1;
        }
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
        var nodes = paragraph.Descendants(Drawing + "t").ToList();
        if (nodes.Count == 0)
        {
            var run = new XElement(Drawing + "r", new XElement(Drawing + "t", text));
            paragraph.Add(run);
            return;
        }
        nodes[0].Value = text;
        foreach (var node in nodes.Skip(1))
        {
            node.Value = string.Empty;
        }
    }

    private static void CopyAttributes(ZipArchiveEntry source, ZipArchiveEntry target)
    {
        target.LastWriteTime = source.LastWriteTime;
        target.ExternalAttributes = source.ExternalAttributes;
    }
}
