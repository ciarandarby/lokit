using System.Buffers.Binary;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Lokit.Office.Core.Protocol;

public enum FrameType : ushort
{
    Hello = 0x0001,
    Ready = 0x0002,
    ExtractRequest = 0x0003,
    ReinsertRequest = 0x0004,
    TranslationUnit = 0x0005,
    TranslationEnd = 0x0006,
    Cancel = 0x0007,
    DocumentStart = 0x0010,
    Unit = 0x0011,
    Warning = 0x0012,
    Progress = 0x0013,
    Result = 0x0014,
    Error = 0x0015,
    Done = 0x0016,
}

public sealed record ProtocolFrame(
    FrameType FrameType,
    Guid RequestId,
    JsonObject Payload,
    bool Required = true,
    ushort Major = ProtocolCodec.ProtocolMajor,
    ushort Minor = ProtocolCodec.ProtocolMinor);

public static class ProtocolCodec
{
    public const ushort ProtocolMajor = 1;
    public const ushort ProtocolMinor = 0;
    private static readonly byte[] Magic = Encoding.ASCII.GetBytes("LOK1");
    private const int HeaderLength = 32;

    public static async Task<ProtocolFrame?> ReadFrameAsync(Stream stream, int maxFrameBytes, CancellationToken cancellationToken)
    {
        var header = new byte[HeaderLength];
        var read = await ReadExactOrEofAsync(stream, header, cancellationToken).ConfigureAwait(false);
        if (!read)
        {
            return null;
        }
        if (!header.AsSpan(0, 4).SequenceEqual(Magic))
        {
            throw new OfficeException("Invalid Office protocol frame magic");
        }
        var major = BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(4, 2));
        var minor = BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(6, 2));
        if (major != ProtocolMajor)
        {
            throw new OfficeException($"Unsupported Office protocol major version: {major}");
        }
        var frameType = (FrameType)BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(8, 2));
        var flags = BinaryPrimitives.ReadUInt16BigEndian(header.AsSpan(10, 2));
        var requestBytes = header.AsSpan(12, 16).ToArray();
        var payloadLength = BinaryPrimitives.ReadUInt32BigEndian(header.AsSpan(28, 4));
        if (payloadLength > maxFrameBytes)
        {
            throw new OfficeException("Office protocol frame exceeds max_frame_bytes");
        }
        var payloadBytes = new byte[payloadLength];
        await ReadExactAsync(stream, payloadBytes, cancellationToken).ConfigureAwait(false);
        var payload = JsonNode.Parse(payloadBytes)?.AsObject()
            ?? throw new OfficeException("Office protocol payload must be a JSON object");
        return new ProtocolFrame(frameType, new Guid(requestBytes), payload, (flags & 1) == 1, major, minor);
    }

    public static async Task WriteFrameAsync(Stream stream, ProtocolFrame frame, int maxFrameBytes, CancellationToken cancellationToken)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(frame.Payload);
        if (payload.Length > maxFrameBytes)
        {
            throw new OfficeException("Office protocol frame exceeds max_frame_bytes");
        }
        var header = new byte[HeaderLength];
        Magic.CopyTo(header, 0);
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(4, 2), frame.Major);
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(6, 2), frame.Minor);
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(8, 2), (ushort)frame.FrameType);
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(10, 2), frame.Required ? (ushort)1 : (ushort)0);
        frame.RequestId.ToByteArray().CopyTo(header, 12);
        BinaryPrimitives.WriteUInt32BigEndian(header.AsSpan(28, 4), (uint)payload.Length);
        await stream.WriteAsync(header, cancellationToken).ConfigureAwait(false);
        await stream.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    private static async Task<bool> ReadExactOrEofAsync(Stream stream, byte[] buffer, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(offset), cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                if (offset == 0)
                {
                    return false;
                }
                throw new OfficeException("Office protocol frame is truncated");
            }
            offset += read;
        }
        return true;
    }

    private static async Task ReadExactAsync(Stream stream, byte[] buffer, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(offset), cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                throw new OfficeException("Office protocol payload is truncated");
            }
            offset += read;
        }
    }
}
