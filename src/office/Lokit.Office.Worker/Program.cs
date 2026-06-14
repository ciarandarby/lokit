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
                    ["worker_version"] = "0.2.2",
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
                    return 0;
                }
                if (frame.FrameType == FrameType.ReinsertRequest)
                {
                    await HandleReinsertAsync(frame, options, cancellationToken).ConfigureAwait(false);
                    return 0;
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
        var result = _extractor.Extract(sourcePath, format, options);
        await WriteAsync(FrameType.DocumentStart, frame.RequestId, new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["format"] = format,
                ["source_fingerprint"] = result.SourceFingerprint,
            },
        }, options, cancellationToken).ConfigureAwait(false);
        foreach (var unit in result.Units)
        {
            await WriteAsync(FrameType.Unit, frame.RequestId, UnitPayload(unit), options, cancellationToken).ConfigureAwait(false);
        }
        await WriteAsync(FrameType.Done, frame.RequestId, new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["units"] = result.Units.Count,
            },
        }, options, cancellationToken).ConfigureAwait(false);
    }

    private async Task HandleReinsertAsync(ProtocolFrame frame, OfficeOptions options, CancellationToken cancellationToken)
    {
        var required = Required(frame.Payload);
        var format = RequiredString(required, "format");
        var sourcePath = RequiredString(required, "source_path");
        var outputPath = RequiredString(required, "output_path");
        var translations = new Dictionary<string, string>(StringComparer.Ordinal);
        while (true)
        {
            var next = await ProtocolCodec.ReadFrameAsync(_input, options.MaxFrameBytes, cancellationToken).ConfigureAwait(false)
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
            translations[RequiredString(unit, "unit_id")] = RequiredString(unit, "target");
        }

        var result = _reinserter.Reinsert(sourcePath, outputPath, format, translations, options);
        await WriteAsync(FrameType.Result, frame.RequestId, new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["units_written"] = result.UnitsWritten,
                ["source_fingerprint"] = result.SourceFingerprint,
                ["output_bytes"] = result.OutputBytes,
            },
        }, options, cancellationToken).ConfigureAwait(false);
        await WriteAsync(FrameType.Done, frame.RequestId, new JsonObject
        {
            ["required"] = new JsonObject
            {
                ["units"] = result.UnitsWritten,
            },
        }, options, cancellationToken).ConfigureAwait(false);
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

    private static JsonObject Required(JsonObject payload)
    {
        return payload["required"]?.AsObject() ?? throw new OfficeException("Office protocol frame is missing required object");
    }

    private static string RequiredString(JsonObject payload, string key)
    {
        return payload[key]?.GetValue<string>() ?? throw new OfficeException($"Office protocol field is missing: {key}");
    }
}
