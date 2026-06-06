from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from lokit import Lokit, LokitJsonContext
from lokit.exporters.csv import export_csv
from lokit.exporters.po import export_po
from lokit.exporters.tmx import export_tmx
from lokit.exporters.xliff import export_xliff
from lokit.exporters.xlsx import export_xlsx
from lokit.importers import stream_tmx
from lokit.data.structure import StreamingStructure
from lokit.parsers.tmx.models import TmxParseMode
from lokit.parsers.tmx.props import TmxProps


@dataclass(slots=True)
class Measurement:
    operation: str
    output_bytes: int
    wall_seconds: float
    cpu_seconds: float
    peak_rss_mb: float
    numba_enabled: bool
    compiled_extensions: int
    python: str
    xoptions: dict[str, bool | str]


def _compiled_extension_count() -> int:
    import lokit

    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if suffix is None:
        return 0
    package_dir = Path(lokit.__file__).parent
    return sum(1 for path in package_dir.rglob("*") if path.name.endswith(suffix))


def _peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def _measure(
    operation: str,
    target: Path,
    callback: Callable[[Path], None],
) -> Measurement:
    TmxProps().status_from_values(["translated", "approved"])
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    callback(target)
    wall_seconds = time.perf_counter() - wall_start
    cpu_seconds = time.process_time() - cpu_start
    output_bytes = target.stat().st_size if target.exists() else 0
    return Measurement(
        operation=operation,
        output_bytes=output_bytes,
        wall_seconds=wall_seconds,
        cpu_seconds=cpu_seconds,
        peak_rss_mb=_peak_rss_mb(),
        numba_enabled=os.environ.get("LOKIT_ENABLE_NUMBA", "").lower()
        in {"1", "true", "yes", "on"},
        compiled_extensions=_compiled_extension_count(),
        python=sys.version,
        xoptions=dict(sys._xoptions),
    )


def _stream_document(source: Path) -> StreamingStructure:
    return stream_tmx(
        str(source),
        source_language="en-US",
        target_language="de-DE",
        mode=TmxParseMode.TEXT_WITH_STATUS,
    )


def _jsonl(
    source: Path,
    selected_context: tuple[LokitJsonContext, ...],
) -> Callable[[Path], None]:
    def write(target: Path) -> None:
        Lokit.to_json(source, target, context=selected_context)

    return write


def _csv(source: Path) -> Callable[[Path], None]:
    def write(target: Path) -> None:
        export_csv(_stream_document(source), target)

    return write


def _xliff(source: Path) -> Callable[[Path], None]:
    def write(target: Path) -> None:
        export_xliff(_stream_document(source), target)

    return write


def _tmx(source: Path) -> Callable[[Path], None]:
    def write(target: Path) -> None:
        export_tmx(_stream_document(source), target)

    return write


def _po(source: Path) -> Callable[[Path], None]:
    def write(target: Path) -> None:
        export_po(_stream_document(source), target)

    return write


def _xlsx(source: Path) -> Callable[[Path], None]:
    def write(target: Path) -> None:
        export_xlsx(_stream_document(source), target)

    return write


OPERATIONS: dict[str, tuple[str, Callable[[Path], Callable[[Path], None]]]] = {
    "jsonl": (".jsonl", lambda source: _jsonl(source, tuple(LokitJsonContext))),
    "jsonl_text": (
        ".jsonl",
        lambda source: _jsonl(
            source,
            (LokitJsonContext.SOURCE, LokitJsonContext.TARGET),
        ),
    ),
    "csv": (".csv", _csv),
    "xliff": (".xliff", _xliff),
    "tmx": (".tmx", _tmx),
    "po": (".po", _po),
    "xlsx": (".xlsx", _xlsx),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--operation", choices=sorted(OPERATIONS), required=True)
    args = parser.parse_args()

    suffix, factory = OPERATIONS[args.operation]
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output) as temp_dir:
        target = Path(temp_dir) / f"{args.operation}{suffix}"
        measurement = _measure(args.operation, target, factory(args.source))

    print(json.dumps(asdict(measurement), sort_keys=True))


if __name__ == "__main__":
    main()
