from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from time import perf_counter

from lokit.importers import import_csv, import_json_i18n, import_tmx, import_xliff

if sys.platform != "win32":
    import resource


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--kind", choices=("json", "csv", "tmx", "xliff"), default="json")
    parser.add_argument("--rich", choices=("none", "first", "last", "all"), default="none")
    parser.add_argument("--label", default="unlabelled")
    parser.add_argument("--count", type=int, default=30_000)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    directory = Path(args.directory)
    count = int(args.count)
    kind = str(args.kind)
    rich = str(args.rich)
    source = directory / (f"en-{rich}.{kind}" if kind in ("tmx", "xliff") else f"en.{kind}")
    target = directory / "fr.json"
    if args.prepare:
        directory.mkdir(parents=True, exist_ok=True)
        if kind == "json":
            source.write_text(json.dumps({f"u{i}": f"Source text {i}" for i in range(count)}), encoding="utf-8")
            target.write_text(json.dumps({f"u{i}": f"Target text {i}" for i in range(count)}), encoding="utf-8")
        elif kind == "csv":
            with source.open("w", encoding="utf-8") as stream:
                stream.write("id,en,fr\n")
                for i in range(count):
                    stream.write(f"u{i},Source text {i},Target text {i}\n")
        else:
            with source.open("w", encoding="utf-8") as stream:
                stream.write(
                    '<tmx version="1.4"><header srclang="en"/><body>'
                    if kind == "tmx"
                    else '<xliff version="1.2"><file original="fixture" source-language="en" '
                    'target-language="fr"><body>'
                )
                for i in range(count):
                    note = (
                        "<note>Keep this note</note>"
                        if rich == "all" or (rich == "first" and i == 0) or (rich == "last" and i == count - 1)
                        else ""
                    )
                    if kind == "tmx":
                        stream.write(
                            f'<tu tuid="u{i}">{note}<tuv xml:lang="en"><seg>Source text {i}</seg></tuv>'
                            f'<tuv xml:lang="fr"><seg>Target text {i}</seg></tuv></tu>'
                        )
                    else:
                        stream.write(
                            f'<trans-unit id="u{i}">{note}<source>Source text {i}</source>'
                            f"<target>Target text {i}</target></trans-unit>"
                        )
                stream.write("</body></tmx>" if kind == "tmx" else "</body></file></xliff>")
        return
    started = perf_counter()
    if kind == "json":
        document = import_json_i18n(
            str(source), source_locale="en", target_locale="fr", target_filepath=str(target), progress=False
        )
    elif kind == "csv":
        document = import_csv(str(source), progress=False)
    elif kind == "tmx":
        document = import_tmx(str(source), source_language="en", target_language="fr", progress=False)
    else:
        document = import_xliff(str(source), progress=False)
    seconds = perf_counter() - started
    peak_mib: float | None = None
    if sys.platform != "win32":
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mib = peak_rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    digest = hashlib.sha256()
    assert len(document.data) == count
    for i, (key, unit) in enumerate(document.data.items()):
        assert (key, unit.source, unit.target) == (f"u{i}", f"Source text {i}", f"Target text {i}")
        if rich == "all" or (rich == "first" and i == 0) or (rich == "last" and i == count - 1):
            assert unit.comments[0].context == "Keep this note"
        digest.update(json.dumps([key, unit.source, unit.target]).encode("utf-8"))
    print(
        json.dumps(
            {
                "label": args.label,
                "kind": kind,
                "rich": rich,
                "count": count,
                "seconds": seconds,
                "peak_mib": peak_mib,
                "sha256": digest.hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
