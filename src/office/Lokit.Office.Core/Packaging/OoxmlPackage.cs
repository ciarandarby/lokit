using System.IO.Compression;
using System.Xml.Linq;

namespace Lokit.Office.Core.Packaging;

public static class OoxmlPackage
{
    private static readonly HashSet<string> DocxMainTypes = new(StringComparer.Ordinal)
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    };

    private static readonly HashSet<string> PptxMainTypes = new(StringComparer.Ordinal)
    {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    };

    public static string DetectFormat(ZipArchive archive, HashSet<string> names)
    {
        if (names.Contains("word/document.xml"))
        {
            return "docx";
        }
        if (names.Contains("ppt/presentation.xml"))
        {
            return "pptx";
        }
        if (names.Contains("xl/workbook.xml"))
        {
            return "xlsx";
        }

        var contentTypes = ReadXml(archive, "[Content_Types].xml");
        foreach (var element in contentTypes.Root?.Elements() ?? Enumerable.Empty<XElement>())
        {
            if (element.Name.LocalName != "Override")
            {
                continue;
            }
            var contentType = (string?)element.Attribute("ContentType") ?? string.Empty;
            if (DocxMainTypes.Contains(contentType))
            {
                return "docx";
            }
            if (PptxMainTypes.Contains(contentType))
            {
                return "pptx";
            }
        }
        throw new OfficeUnsupportedPackageException("Unsupported OOXML package type");
    }

    public static XDocument ReadXml(ZipArchive archive, string part)
    {
        var entry = archive.GetEntry(part) ?? throw new OfficePackageException($"Missing Office part: {part}");
        using var stream = entry.Open();
        try
        {
            return XDocument.Load(stream, LoadOptions.PreserveWhitespace);
        }
        catch (Exception exc) when (exc is System.Xml.XmlException or InvalidOperationException)
        {
            throw new OfficePackageException($"Malformed XML in Office part {part}", exc);
        }
    }

    public static IReadOnlyList<string> DocxParts(HashSet<string> names, OfficeOptions options)
    {
        var parts = new List<string> { "word/document.xml" };
        if (options.IncludeHeadersFooters)
        {
            parts.AddRange(names.Where(name => (name.StartsWith("word/header", StringComparison.Ordinal) || name.StartsWith("word/footer", StringComparison.Ordinal)) && name.EndsWith(".xml", StringComparison.Ordinal)).Order(StringComparer.Ordinal));
        }
        if (options.IncludeComments && names.Contains("word/comments.xml"))
        {
            parts.Add("word/comments.xml");
        }
        foreach (var name in new[] { "word/footnotes.xml", "word/endnotes.xml" })
        {
            if (names.Contains(name))
            {
                parts.Add(name);
            }
        }
        return parts;
    }

    public static IReadOnlyList<string> PptxParts(ZipArchive archive, HashSet<string> names, OfficeOptions options)
    {
        var parts = new List<string>();
        parts.AddRange(names.Where(name => name.StartsWith("ppt/slides/slide", StringComparison.Ordinal) && name.EndsWith(".xml", StringComparison.Ordinal)).OrderBy(SlideNumber));
        if (options.IncludeNotes)
        {
            parts.AddRange(names.Where(name => name.StartsWith("ppt/notesSlides/notesSlide", StringComparison.Ordinal) && name.EndsWith(".xml", StringComparison.Ordinal)).OrderBy(SlideNumber));
        }
        if (options.IncludeMasterLayoutContent)
        {
            parts.AddRange(names.Where(name => (name.StartsWith("ppt/slideLayouts/", StringComparison.Ordinal) || name.StartsWith("ppt/slideMasters/", StringComparison.Ordinal)) && name.EndsWith(".xml", StringComparison.Ordinal)).Order(StringComparer.Ordinal));
        }
        return parts;
    }

    public static int SlideNumber(string part)
    {
        var stem = Path.GetFileNameWithoutExtension(part);
        var digits = new string(stem.Where(char.IsDigit).ToArray());
        return int.TryParse(digits, out var number) ? number : 0;
    }
}
