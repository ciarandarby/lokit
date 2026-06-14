using System.IO.Compression;
using System.Security.Cryptography;

namespace Lokit.Office.Core.Packaging;

public sealed record PackageInfo(string Fingerprint, HashSet<string> Names);

public static class PackagePreflight
{
    public static PackageInfo Inspect(string path, OfficeOptions options)
    {
        var fingerprint = Fingerprint(path, options.MaxCompressedBytes);
        using var archive = ZipFile.OpenRead(path);
        if (archive.Entries.Count > options.MaxZipEntries)
        {
            throw new OfficePackageException("Office package has too many ZIP entries");
        }

        var names = new HashSet<string>(StringComparer.Ordinal);
        long compressed = 0;
        long uncompressed = 0;
        foreach (var entry in archive.Entries)
        {
            var normalized = NormalizePartName(entry.FullName);
            if (normalized.Length == 0 || normalized != entry.FullName)
            {
                throw new OfficePackageException($"Unsafe Office ZIP entry name: {entry.FullName}");
            }
            if (!names.Add(normalized))
            {
                throw new OfficePackageException($"Duplicate Office ZIP entry: {normalized}");
            }
            compressed += entry.CompressedLength;
            uncompressed += entry.Length;
            if (compressed > options.MaxCompressedBytes)
            {
                throw new OfficePackageException("Office package exceeds max_compressed_bytes");
            }
            if (uncompressed > options.MaxUncompressedBytes)
            {
                throw new OfficePackageException("Office package exceeds max_uncompressed_bytes");
            }
            if (entry.CompressedLength > 0 && (double)entry.Length / entry.CompressedLength > options.MaxCompressionRatio)
            {
                throw new OfficePackageException($"Suspicious compression ratio in Office ZIP entry: {normalized}");
            }
        }
        if (!names.Contains("[Content_Types].xml"))
        {
            throw new OfficePackageException("Office package is missing [Content_Types].xml");
        }
        return new PackageInfo(fingerprint, names);
    }

    public static string NormalizePartName(string name)
    {
        if (name.StartsWith("/", StringComparison.Ordinal) || name.Contains('\\', StringComparison.Ordinal) || name.Contains('\0', StringComparison.Ordinal))
        {
            return string.Empty;
        }
        var parts = new Stack<string>();
        foreach (var rawPart in name.Split('/'))
        {
            if (rawPart.Length == 0 || rawPart == ".")
            {
                continue;
            }
            if (rawPart == "..")
            {
                if (parts.Count == 0)
                {
                    return string.Empty;
                }
                parts.Pop();
                continue;
            }
            parts.Push(rawPart);
        }
        if (parts.Count == 0)
        {
            return string.Empty;
        }
        return string.Join("/", parts.Reverse());
    }

    private static string Fingerprint(string path, long maxBytes)
    {
        using var sha = SHA256.Create();
        using var stream = File.OpenRead(path);
        var buffer = new byte[1024 * 1024];
        long readTotal = 0;
        while (true)
        {
            var read = stream.Read(buffer, 0, buffer.Length);
            if (read == 0)
            {
                break;
            }
            readTotal += read;
            if (readTotal > maxBytes)
            {
                throw new OfficePackageException("Office source exceeds max_compressed_bytes");
            }
            sha.TransformBlock(buffer, 0, read, null, 0);
        }
        sha.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
        return $"sha256:{Convert.ToHexString(sha.Hash!).ToLowerInvariant()}";
    }
}
