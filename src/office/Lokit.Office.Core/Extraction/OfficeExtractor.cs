using System.IO.Compression;
using System.Xml.Linq;
using Lokit.Office.Core.Packaging;

namespace Lokit.Office.Core.Extraction;

public sealed class OfficeExtractor
{
    private static readonly XNamespace Word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    private static readonly XNamespace Drawing = "http://schemas.openxmlformats.org/drawingml/2006/main";

    public ExtractionResult Extract(string sourcePath, string expectedFormat, OfficeOptions options)
    {
        var package = PackagePreflight.Inspect(sourcePath, options);
        using var archive = ZipFile.OpenRead(sourcePath);
        var actualFormat = OoxmlPackage.DetectFormat(archive, package.Names);
        if (actualFormat != expectedFormat)
        {
            throw new OfficeUnsupportedPackageException($"Expected {expectedFormat.ToUpperInvariant()} package, detected {actualFormat.ToUpperInvariant()}");
        }

        var parts = expectedFormat == "docx"
            ? OoxmlPackage.DocxParts(package.Names, options)
            : OoxmlPackage.PptxParts(archive, package.Names, options);
        var units = new List<OfficeUnit>();
        foreach (var part in parts)
        {
            if (!package.Names.Contains(part))
            {
                continue;
            }
            var xml = OoxmlPackage.ReadXml(archive, part);
            if (expectedFormat == "docx")
            {
                units.AddRange(ExtractDocxPart(xml, part, package.Fingerprint, options));
            }
            else
            {
                units.AddRange(ExtractPptxPart(xml, part, package.Fingerprint, options));
            }
        }
        return new ExtractionResult(expectedFormat, package.Fingerprint, units, Array.Empty<OfficeWarning>());
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
                if (text.Length > options.MaxTextUnitChars)
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
        var container = PptxContainer(part);
        var index = 0;
        foreach (var paragraph in xml.Descendants(Drawing + "p"))
        {
            var text = PptxParagraphText(paragraph);
            if (!string.IsNullOrWhiteSpace(text))
            {
                if (text.Length > options.MaxTextUnitChars)
                {
                    throw new OfficePackageException("PPTX text unit exceeds max_text_unit_chars");
                }
                var extensions = Extensions("pptx", part, container, fingerprint);
                var slideNumber = OoxmlPackage.SlideNumber(part);
                if (slideNumber > 0)
                {
                    extensions["office.slide_number"] = slideNumber.ToString(System.Globalization.CultureInfo.InvariantCulture);
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
        return string.Concat(paragraph.Descendants(Drawing + "t").Select(element => element.Value));
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
        return Path.GetFileNameWithoutExtension(part);
    }
}
