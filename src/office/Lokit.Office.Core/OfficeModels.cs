namespace Lokit.Office.Core;

public sealed record OfficeUnit(
    string UnitId,
    string Source,
    string Part,
    Dictionary<string, string> Extensions);

public sealed record OfficeWarning(
    string Code,
    string Message,
    string? UnitId = null,
    string? Part = null);

public sealed record ExtractionResult(
    string Format,
    string SourceFingerprint,
    IReadOnlyList<OfficeUnit> Units,
    IReadOnlyList<OfficeWarning> Warnings);

public sealed record ReinsertionResult(
    int UnitsWritten,
    IReadOnlyList<OfficeWarning> Warnings,
    string SourceFingerprint,
    long OutputBytes);
