from __future__ import annotations

import argparse
import json
import statistics
import sys
from time import perf_counter

from lokit._interchange_rust import materialize_interchange_bytes

if sys.platform != "win32":
    import resource


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("tmx", "xliff"), default="tmx")
    parser.add_argument("--preamble-mib", type=int, default=24)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--label", default="unlabelled")
    args = parser.parse_args()
    if not 1 <= args.preamble_mib <= 32 or args.repeats < 1:
        parser.error("preamble-mib must be in 1..32 and repeats must be positive")
    body = (
        b'<tmx><header srclang="en"/><body><tu tuid="u"><tuv xml:lang="en"><seg>Source</seg></tuv></tu></body></tmx>'
        if args.format == "tmx"
        else b'<xliff version="1.2"><file source-language="en"><body><trans-unit id="u">'
        b"<source>Source</source></trans-unit></body></file></xliff>"
    )
    payload = b"".join([b"<!--", *([b"x" * 1024] * (args.preamble_mib * 1024)), b"-->", body])
    durations: list[float] = []
    for repetition in range(args.repeats + 1):
        started = perf_counter()
        document = materialize_interchange_bytes(payload, args.format)
        seconds = perf_counter() - started
        assert list(document.data) == ["u"]
        assert document.source_locale == "en"
        assert document.data["u"].source == "Source"
        if repetition:
            durations.append(seconds)
    peak_mib: float | None = None
    if sys.platform != "win32":
        peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
            1024 * 1024 if sys.platform == "darwin" else 1024
        )
    print(
        json.dumps(
            {
                "label": args.label,
                "format": args.format,
                "input_bytes": len(payload),
                "median_seconds": statistics.median(durations),
                "peak_mib": peak_mib,
            }
        )
    )


if __name__ == "__main__":
    main()
