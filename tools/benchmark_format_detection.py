from __future__ import annotations

import argparse
import json
import statistics
import sys
import tracemalloc
import zipfile
from pathlib import Path
from time import perf_counter

from lokit.format_detection import LokitInputFormat, detect_format, detect_format_from_bytes

if sys.platform != "win32":
    import resource


def prepare(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "late.json").write_text(
        '{"ignored":"' + "x" * 2_000_000 + '","data":{"u":{"source":"A"}},"source_locale":"en"}',
        encoding="utf-8",
    )
    (directory / "early.json").write_text(
        '{"source_locale":"en","data":{"u":{"source":"A","target":"' + "x" * 2_000_000 + '"}}}',
        encoding="utf-8",
    )
    (directory / "preamble.xml").write_text("<!--" + "x" * 2_000_000 + '--><tmx version="1.4"/>', encoding="utf-8")
    with zipfile.ZipFile(directory / "many.docx", "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", b"wordprocessingml.document.main+xml")
        archive.writestr("word/document.xml", b"<document/>")
        for index in range(20_000):
            archive.writestr(f"media/{index}.txt", b"")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--bytes", action="store_true")
    parser.add_argument("--expected", default="lokit_json")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--label", default="current")
    args = parser.parse_args()
    path = Path(args.path)
    if args.prepare:
        prepare(path)
        return
    expected = LokitInputFormat(str(args.expected))
    data = path.read_bytes() if args.bytes else None

    def detect() -> LokitInputFormat:
        return detect_format_from_bytes(data) if data is not None else detect_format(path)

    assert detect() is expected
    durations: list[float] = []
    for _ in range(int(args.repeats)):
        started = perf_counter()
        detected = detect()
        durations.append(perf_counter() - started)
        assert detected is expected
    peak_mib: float | None = None
    if sys.platform != "win32":
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mib = rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    tracemalloc.start()
    assert detect() is expected
    _, python_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(
        json.dumps(
            {
                "label": args.label,
                "fixture": path.name,
                "mode": "bytes" if data is not None else "path",
                "format": str(expected),
                "seconds": statistics.median(durations),
                "samples_seconds": durations,
                "peak_mib": peak_mib,
                "python_peak_bytes": python_peak,
            }
        )
    )


if __name__ == "__main__":
    main()
