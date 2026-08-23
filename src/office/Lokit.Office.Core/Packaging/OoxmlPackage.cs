using System.IO.Compression;
using System.Xml.Linq;

namespace Lokit.Office.Core.Packaging;

public static class OoxmlPackage
{
    private static readonly XNamespace Presentation = "http://schemas.openxmlformats.org/presentationml/2006/main";
    private static readonly XNamespace OfficeRelationships =
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
    private static readonly XNamespace PackageRelationships =
        "http://schemas.openxmlformats.org/package/2006/relationships";
    private static readonly HashSet<string> DocxMainTypes = new(StringComparer.Ordinal)
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    };

    private static readonly HashSet<string> PptxMainTypes = new(StringComparer.Ordinal)
    {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    };

    public static string DetectFormat(ZipArchive archive, HashSet<string> names, OfficeOptions options)
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

        var contentTypes = ReadXml(archive, "[Content_Types].xml", options.MaxUnitBytes);
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

    public static XDocument ReadXml(ZipArchive archive, string part, long maxUnitBytes)
    {
        if (maxUnitBytes < 1)
        {
            throw new OfficePackageException("max_unit_bytes must be at least 1");
        }
        var entry = archive.GetEntry(part) ?? throw new OfficePackageException($"Missing Office part: {part}");
        if (entry.Length > maxUnitBytes)
        {
            throw new OfficePackageException($"Office XML part exceeds max_unit_bytes: {part}");
        }
        using var stream = entry.Open();
        using var bounded = new BoundedReadStream(stream, maxUnitBytes, part);
        try
        {
            return XDocument.Load(bounded, LoadOptions.PreserveWhitespace);
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
        var slides = PresentationSlideParts(archive, names, options);
        if (slides.Count == 0)
        {
            slides.AddRange(MatchingParts(names, "ppt/slides/slide", numeric: true));
        }
        var notes = MatchingParts(names, "ppt/notesSlides/notesSlide", numeric: true);
        var layouts = RelatedParts(archive, names, slides, "/slideLayout", options);
        if (layouts.Count == 0)
        {
            layouts.AddRange(MatchingParts(names, "ppt/slideLayouts/slideLayout"));
        }
        var masters = RelatedParts(archive, names, layouts, "/slideMaster", options);
        if (masters.Count == 0)
        {
            masters.AddRange(MatchingParts(names, "ppt/slideMasters/slideMaster"));
        }
        var notesMasters = RelatedParts(archive, names, notes, "/notesMaster", options);
        if (notesMasters.Count == 0)
        {
            notesMasters.AddRange(MatchingParts(names, "ppt/notesMasters/notesMaster"));
        }

        if (options.IncludeSlides || options.IncludeAltText)
        {
            AddDistinct(parts, slides);
        }
        if ((options.IncludeSpeakerNotes && options.IncludeNotes) || options.IncludeAltText)
        {
            AddDistinct(parts, notes);
        }
        if ((options.IncludeSlideLayouts && options.IncludeMasterLayoutContent) || options.IncludeAltText)
        {
            AddDistinct(parts, layouts);
        }
        if ((options.IncludeSlideMasters && options.IncludeMasterLayoutContent) || options.IncludeAltText)
        {
            AddDistinct(parts, masters);
        }
        if (options.IncludeNotesMasters || options.IncludeAltText)
        {
            AddDistinct(parts, notesMasters);
        }
        if (options.IncludeHandoutMasters || options.IncludeAltText)
        {
            AddDistinct(parts, MatchingParts(names, "ppt/handoutMasters/handoutMaster"));
        }
        if (options.IncludeComments)
        {
            AddDistinct(parts, MatchingParts(names, "ppt/comments/comment"));
        }
        if (options.IncludeCharts)
        {
            AddDistinct(parts, MatchingParts(names, "ppt/charts/chart", numeric: true));
        }
        if (options.IncludeDiagrams)
        {
            AddDistinct(parts, MatchingParts(names, "ppt/diagrams/data", numeric: true));
        }
        if (options.IncludeDocumentMetadata)
        {
            AddDistinct(parts, MatchingParts(names, "docProps/core"));
            AddDistinct(parts, MatchingParts(names, "docProps/custom"));
        }
        return parts;
    }

    private static List<string> PresentationSlideParts(
        ZipArchive archive,
        HashSet<string> names,
        OfficeOptions options)
    {
        if (!names.Contains("ppt/presentation.xml") || !names.Contains("ppt/_rels/presentation.xml.rels"))
        {
            return new List<string>();
        }
        var relationships = Relationships(archive, "ppt/presentation.xml", names, options);
        var presentation = ReadXml(archive, "ppt/presentation.xml", options.MaxUnitBytes);
        var parts = new List<string>();
        foreach (var slideId in presentation.Descendants(Presentation + "sldId"))
        {
            var relationshipId = (string?)slideId.Attribute(OfficeRelationships + "id");
            if (relationshipId is not null && relationships.TryGetValue(relationshipId, out var part))
            {
                AddDistinct(parts, new[] { part });
            }
        }
        return parts;
    }

    private static List<string> RelatedParts(
        ZipArchive archive,
        HashSet<string> names,
        IEnumerable<string> sourceParts,
        string relationshipSuffix,
        OfficeOptions options)
    {
        var parts = new List<string>();
        foreach (var sourcePart in sourceParts)
        {
            var relationships = Relationships(archive, sourcePart, names, options);
            var relationshipPart = RelationshipPart(sourcePart);
            if (!names.Contains(relationshipPart))
            {
                continue;
            }
            var document = ReadXml(archive, relationshipPart, options.MaxUnitBytes);
            foreach (var relationship in document.Descendants(PackageRelationships + "Relationship"))
            {
                var type = (string?)relationship.Attribute("Type") ?? string.Empty;
                var relationshipId = (string?)relationship.Attribute("Id");
                if (relationshipId is not null && type.EndsWith(relationshipSuffix, StringComparison.Ordinal) &&
                    relationships.TryGetValue(relationshipId, out var target))
                {
                    AddDistinct(parts, new[] { target });
                }
            }
        }
        return parts;
    }

    private static Dictionary<string, string> Relationships(
        ZipArchive archive,
        string sourcePart,
        HashSet<string> names,
        OfficeOptions options)
    {
        var relationshipPart = RelationshipPart(sourcePart);
        var relationships = new Dictionary<string, string>(StringComparer.Ordinal);
        if (!names.Contains(relationshipPart))
        {
            return relationships;
        }
        var document = ReadXml(archive, relationshipPart, options.MaxUnitBytes);
        foreach (var relationship in document.Descendants(PackageRelationships + "Relationship"))
        {
            var id = (string?)relationship.Attribute("Id");
            var target = (string?)relationship.Attribute("Target");
            var mode = (string?)relationship.Attribute("TargetMode");
            if (id is not null && target is not null && mode != "External")
            {
                var resolved = ResolveTarget(sourcePart, target);
                if (names.Contains(resolved))
                {
                    relationships[id] = resolved;
                }
            }
        }
        return relationships;
    }

    private static string RelationshipPart(string sourcePart)
    {
        var directory = Path.GetDirectoryName(sourcePart)?.Replace('\\', '/') ?? string.Empty;
        var filename = Path.GetFileName(sourcePart);
        return string.IsNullOrEmpty(directory)
            ? $"_rels/{filename}.rels"
            : $"{directory}/_rels/{filename}.rels";
    }

    private static string ResolveTarget(string sourcePart, string target)
    {
        if (target.StartsWith("/", StringComparison.Ordinal))
        {
            return target.TrimStart('/');
        }
        var segments = new List<string>();
        var directory = Path.GetDirectoryName(sourcePart)?.Replace('\\', '/') ?? string.Empty;
        segments.AddRange(directory.Split('/', StringSplitOptions.RemoveEmptyEntries));
        foreach (var segment in target.Split('/', StringSplitOptions.RemoveEmptyEntries))
        {
            if (segment == ".")
            {
                continue;
            }
            if (segment == "..")
            {
                if (segments.Count == 0)
                {
                    throw new OfficePackageException($"Relationship target escapes package: {target}");
                }
                segments.RemoveAt(segments.Count - 1);
                continue;
            }
            segments.Add(segment);
        }
        return string.Join('/', segments);
    }

    private static IEnumerable<string> MatchingParts(HashSet<string> names, string prefix, bool numeric = false)
    {
        var matches = names.Where(name =>
            name.StartsWith(prefix, StringComparison.Ordinal) &&
            name.EndsWith(".xml", StringComparison.Ordinal) &&
            !name.Contains("/_rels/", StringComparison.Ordinal));
        return numeric ? matches.OrderBy(SlideNumber) : matches.Order(StringComparer.Ordinal);
    }

    private static void AddDistinct(List<string> parts, IEnumerable<string> candidates)
    {
        foreach (var candidate in candidates)
        {
            if (!parts.Contains(candidate, StringComparer.Ordinal))
            {
                parts.Add(candidate);
            }
        }
    }

    public static int SlideNumber(string part)
    {
        var stem = Path.GetFileNameWithoutExtension(part);
        var digits = new string(stem.Where(char.IsDigit).ToArray());
        return int.TryParse(digits, out var number) ? number : 0;
    }

    private sealed class BoundedReadStream : Stream
    {
        private readonly Stream _inner;
        private readonly long _limit;
        private readonly string _part;
        private long _read;

        public BoundedReadStream(Stream inner, long limit, string part)
        {
            _inner = inner;
            _limit = limit;
            _part = part;
        }

        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position
        {
            get => throw new NotSupportedException();
            set => throw new NotSupportedException();
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            var read = _inner.Read(buffer, offset, AllowedCount(count));
            Account(read);
            return read;
        }

        public override int Read(Span<byte> buffer)
        {
            var read = _inner.Read(buffer[..AllowedCount(buffer.Length)]);
            Account(read);
            return read;
        }

        public override int ReadByte()
        {
            Span<byte> value = stackalloc byte[1];
            return Read(value) == 0 ? -1 : value[0];
        }

        private int AllowedCount(int requested)
        {
            if (requested < 1)
            {
                return requested;
            }
            var remaining = _limit - _read;
            if (remaining >= requested)
            {
                return requested;
            }
            return (int)Math.Min(remaining + 1, int.MaxValue);
        }

        private void Account(int read)
        {
            _read += read;
            if (_read > _limit)
            {
                throw new OfficePackageException($"Office XML part exceeds max_unit_bytes: {_part}");
            }
        }

        public override void Flush() => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    }
}
