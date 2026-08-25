using System.Text.Json.Nodes;
using Lokit.Office.Core;
using Lokit.Office.Core.Extraction;
using Lokit.Office.Core.Protocol;
using Lokit.Office.Core.Reinsertion;

var worker = new WorkerCommandLoop(Console.OpenStandardInput(), Console.OpenStandardOutput(), Console.Error);
return await worker.RunAsync(CancellationToken.None).ConfigureAwait(false);

internal sealed class WorkerCommandLoop
{
    private readonly Stream _input;
    private readonly Stream _output;
    private readonly TextWriter _diagnostics;
    private readonly OfficeExtractor _extractor = new();
    private readonly OfficeReinserter _reinserter = new();

    public WorkerCommandLoop(Stream input, Stream output, TextWriter diagnostics)
    {
        _input = input;
        _output = output;
        _diagnostics = diagnostics;
    }

    public async Task<int> RunAsync(CancellationToken cancellationToken)
    {
        var options = new OfficeOptions();
        try
        {
            var hello = await ProtocolCodec.ReadFrameAsync(_input, options.MaxFrameBytes, cancellationToken).ConfigureAwait(false);
            if (hello is null || hello.FrameType != FrameType.Hello)
            {
                throw new OfficeException("Office worker expected hello frame");
            }
            await WriteAsync(FrameType.Ready, hello.RequestId, new JsonObject
            {
                ["required"] = new JsonObject
                {
                    ["worker"] = "lokit-office",
                    ["worker_version"] = "0.5.3",
                    ["protocol_major"] = ProtocolCodec.ProtocolMajor,
                    ["protocol_minor"] = ProtocolCodec.ProtocolMinor,
                },
            }, options, cancellationToken).ConfigureAwait(false);

            while (true)
            {
                var frame = await ProtocolCodec.ReadFrameAsync(_input, options.MaxFrameBytes, cancellationToken).ConfigureAwait(false);
                if (frame is null)
                {
                    return 0;
                }
                if (frame.FrameType == FrameType.ExtractRequest)
                {
                    await HandleExtractAsync(frame, options, cancellationToken).ConfigureAwait(false);
                    continue;
                }
                if (frame.FrameType == FrameType.ReinsertRequest)
                {
                    await HandleReinsertAsync(frame, options, cancellationToken).ConfigureAwait(false);
                    continue;
                }
                if (frame.FrameType == FrameType.Cancel)
                {
                    return 2;
                }
                throw new OfficeException($"Unexpected Office protocol frame: {frame.FrameType}");
            }
        }
        catch (Exception exc)
        {
            await _diagnostics.WriteLineAsync(exc.ToString()).ConfigureAwait(false);
            return 1;
        }
    }

    private async Task HandleExtractAsync(ProtocolFrame frame, OfficeOptions options, CancellationToken cancellationToken)
    {
        var required = Required(frame.Payload);
        var format = RequiredString(required, "format");
        var sourcePath = RequiredString(required, "source_path");
        var requestOptions = RequestOptions(required, options);
        var result = _extractor.ExtractStreaming(sourcePath, format, requestOptions);
        await WriteAsync(FrameType.DocumentStart, frame.RequestId, new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["format"] = format,
                ["source_fingerprint"] = result.SourceFingerprint,
            },
        }, requestOptions, cancellationToken).ConfigureAwait(false);
        foreach (var warning in result.Warnings)
        {
            await WriteAsync(
                FrameType.Warning,
                frame.RequestId,
                WarningPayload(warning),
                requestOptions,
                cancellationToken).ConfigureAwait(false);
        }
        var units = 0;
        foreach (var unit in result.Units)
        {
            await WriteAsync(FrameType.Unit, frame.RequestId, UnitPayload(unit), requestOptions, cancellationToken).ConfigureAwait(false);
            units += 1;
        }
        await WriteAsync(FrameType.Done, frame.RequestId, new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["units"] = units,
            },
        }, requestOptions, cancellationToken).ConfigureAwait(false);
    }

    private async Task HandleReinsertAsync(ProtocolFrame frame, OfficeOptions options, CancellationToken cancellationToken)
    {
        var required = Required(frame.Payload);
        var format = RequiredString(required, "format");
        var sourcePath = RequiredString(required, "source_path");
        var outputPath = RequiredString(required, "output_path");
        var requestOptions = RequestOptions(required, options);
        var translations = new Dictionary<string, string>(StringComparer.Ordinal);
        while (true)
        {
            var next = await ProtocolCodec.ReadFrameAsync(_input, requestOptions.MaxFrameBytes, cancellationToken).ConfigureAwait(false)
                ?? throw new OfficeException("Office protocol ended before translation_end");
            if (next.RequestId != frame.RequestId)
            {
                throw new OfficeException("Office protocol request ID mismatch");
            }
            if (next.FrameType == FrameType.TranslationEnd)
            {
                break;
            }
            if (next.FrameType != FrameType.TranslationUnit)
            {
                throw new OfficeException($"Unexpected Office reinsertion frame: {next.FrameType}");
            }
            var unit = Required(next.Payload);
            var target = RequiredString(unit, "target");
            if (TextLimits.ExceedsUnicodeScalarLimit(target, requestOptions.MaxTextUnitChars))
            {
                throw new OfficeReinsertionException("Office translation exceeds max_text_unit_chars");
            }
            translations[RequiredString(unit, "unit_id")] = target;
        }

        var result = _reinserter.Reinsert(sourcePath, outputPath, format, translations, requestOptions);
        foreach (var warning in result.Warnings)
        {
            await WriteAsync(
                FrameType.Warning,
                frame.RequestId,
                WarningPayload(warning),
                requestOptions,
                cancellationToken).ConfigureAwait(false);
        }
        await WriteAsync(FrameType.Result, frame.RequestId, new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["units_written"] = result.UnitsWritten,
                ["source_fingerprint"] = result.SourceFingerprint,
                ["output_bytes"] = result.OutputBytes,
            },
        }, requestOptions, cancellationToken).ConfigureAwait(false);
        await WriteAsync(FrameType.Done, frame.RequestId, new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["units"] = result.UnitsWritten,
            },
        }, requestOptions, cancellationToken).ConfigureAwait(false);
    }

    private async Task WriteAsync(FrameType frameType, Guid requestId, JsonObject payload, OfficeOptions options, CancellationToken cancellationToken)
    {
        await ProtocolCodec.WriteFrameAsync(
            _output,
            new ProtocolFrame(frameType, requestId, payload),
            options.MaxFrameBytes,
            cancellationToken).ConfigureAwait(false);
    }

    private static JsonObject UnitPayload(OfficeUnit unit)
    {
        var extensions = new JsonObject();
        foreach (var item in unit.Extensions)
        {
            extensions[item.Key] = item.Value;
        }
        return new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["unit_id"] = unit.UnitId,
                ["source"] = unit.Source,
                ["status"] = "unknown",
            },
            ["optional"] = new JsonObject
            {
                ["target"] = null,
                ["extensions"] = extensions,
            },
        };
    }

    private static JsonObject WarningPayload(OfficeWarning warning)
    {
        return new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["code"] = warning.Code,
                ["message"] = warning.Message,
            },
            ["optional"] = new JsonObject
            {
                ["unit_id"] = warning.UnitId,
                ["part"] = warning.Part,
            },
        };
    }

    private static JsonObject Required(JsonObject payload)
    {
        return payload["required"]?.AsObject() ?? throw new OfficeException("Office protocol frame is missing required object");
    }

    private static string RequiredString(JsonObject payload, string key)
    {
        return payload[key]?.GetValue<string>() ?? throw new OfficeException($"Office protocol field is missing: {key}");
    }

    private static OfficeOptions RequestOptions(JsonObject required, OfficeOptions defaults)
    {
        if (required["options"] is not JsonObject values)
        {
            return defaults;
        }
        return defaults with
        {
            MaxFrameBytes = Option(values, "max_frame_bytes", defaults.MaxFrameBytes),
            MaxUnitBytes = Option(values, "max_unit_bytes", defaults.MaxUnitBytes),
            MaxZipEntries = Option(values, "max_zip_entries", defaults.MaxZipEntries),
            MaxCompressedBytes = Option(values, "max_compressed_bytes", defaults.MaxCompressedBytes),
            MaxUncompressedBytes = Option(values, "max_uncompressed_bytes", defaults.MaxUncompressedBytes),
            MaxCompressionRatio = Option(values, "max_compression_ratio", defaults.MaxCompressionRatio),
            MaxTextUnitChars = Option(values, "max_text_unit_chars", defaults.MaxTextUnitChars),
            IncludeHeadersFooters = Option(values, "include_headers_footers", defaults.IncludeHeadersFooters),
            IncludeComments = Option(values, "include_comments", defaults.IncludeComments),
            IncludeSlides = Option(values, "include_slides", defaults.IncludeSlides),
            IncludeSpeakerNotes = Option(values, "include_speaker_notes", defaults.IncludeSpeakerNotes),
            IncludeNotes = Option(values, "include_notes", defaults.IncludeNotes),
            IncludeSlideMasters = Option(values, "include_slide_masters", defaults.IncludeSlideMasters),
            IncludeSlideLayouts = Option(values, "include_slide_layouts", defaults.IncludeSlideLayouts),
            IncludeNotesMasters = Option(values, "include_notes_masters", defaults.IncludeNotesMasters),
            IncludeHandoutMasters = Option(values, "include_handout_masters", defaults.IncludeHandoutMasters),
            IncludeMasterLayoutContent = Option(
                values,
                "include_master_layout_content",
                defaults.IncludeMasterLayoutContent),
            IncludeAltText = Option(values, "include_alt_text", defaults.IncludeAltText),
            IncludeCharts = Option(values, "include_charts", defaults.IncludeCharts),
            IncludeDiagrams = Option(values, "include_diagrams", defaults.IncludeDiagrams),
            IncludeDocumentMetadata = Option(
                values,
                "include_document_metadata",
                defaults.IncludeDocumentMetadata),
            IncludeHiddenSlides = Option(values, "include_hidden_slides", defaults.IncludeHiddenSlides),
            MissingTranslationPolicy = Option(
                values,
                "missing_translation_policy",
                defaults.MissingTranslationPolicy),
            ExtraTranslationPolicy = Option(values, "extra_translation_policy", defaults.ExtraTranslationPolicy),
        };
    }

    private static T Option<T>(JsonObject values, string key, T fallback)
    {
        var value = values[key];
        if (value is null)
        {
            return fallback;
        }
        try
        {
            return value.GetValue<T>();
        }
        catch (InvalidOperationException exc)
        {
            throw new OfficeException($"Office protocol option has an invalid type: {key}", exc);
        }
    }
}
