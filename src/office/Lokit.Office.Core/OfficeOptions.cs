namespace Lokit.Office.Core;

public sealed record OfficeOptions
{
    public int MaxFrameBytes { get; init; } = 16 * 1024 * 1024;
    public long MaxUnitBytes { get; init; } = 8 * 1024 * 1024;
    public int MaxZipEntries { get; init; } = 10_000;
    public long MaxCompressedBytes { get; init; } = 1L * 1024 * 1024 * 1024;
    public long MaxUncompressedBytes { get; init; } = 4L * 1024 * 1024 * 1024;
    public double MaxCompressionRatio { get; init; } = 100.0;
    public int MaxTextUnitChars { get; init; } = 1_000_000;
    public bool IncludeHeadersFooters { get; init; } = true;
    public bool IncludeComments { get; init; } = true;
    public bool IncludeNotes { get; init; } = true;
    public bool IncludeMasterLayoutContent { get; init; }
    public bool IncludeAltText { get; init; } = true;
    public string MissingTranslationPolicy { get; init; } = "preserve";
    public string ExtraTranslationPolicy { get; init; } = "warn";
}
