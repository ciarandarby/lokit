from __future__ import annotations

from pathlib import Path

import pytest

import lokit
from lokit.data.structure import Data, StreamingStructure
from lokit.format_detection import LokitInputFormat
from lokit.types import DictField


@pytest.mark.parametrize(
    ("input_format", "stream_name", "expected_args", "expected_kwargs"),
    [
        (LokitInputFormat.LOKIT, "stream_lokit", ("input.bin",), {}),
        (LokitInputFormat.LOKIT_JSON, "stream_lokit_json", ("input.bin",), {}),
        (
            LokitInputFormat.TMX,
            "stream_tmx",
            ("input.bin",),
            {"source_language": "en-US", "target_language": "fr-FR", "domain": "web"},
        ),
        (LokitInputFormat.XLIFF, "stream_xliff", ("input.bin",), {}),
        (LokitInputFormat.CSV, "stream_csv", ("input.bin", "en-US", "fr-FR"), {}),
        (LokitInputFormat.XLSX, "stream_xlsx", ("input.bin", "en-US", "fr-FR"), {}),
        (LokitInputFormat.DOCX, "stream_docx", ("input.bin", "en-US", "fr-FR"), {}),
        (LokitInputFormat.PPTX, "stream_pptx", ("input.bin", "en-US", "fr-FR"), {}),
        (LokitInputFormat.HTML, "stream_html", ("input.bin", "en-US", "fr-FR"), {}),
        (LokitInputFormat.PO, "stream_po", ("input.bin", "en-US", "fr-FR"), {}),
        (LokitInputFormat.JSON_I18N, "stream_json_i18n", ("input.bin", "en-US", "fr-FR"), {}),
        (LokitInputFormat.IDML, "stream_idml", ("input.bin", "en-US", "fr-FR"), {}),
    ],
)
def test_to_dict_dispatches_every_streaming_input_format(
    monkeypatch: pytest.MonkeyPatch,
    input_format: LokitInputFormat,
    stream_name: str,
    expected_args: tuple[object, ...],
    expected_kwargs: dict[str, object],
) -> None:
    import lokit.format_detection
    import lokit.importers

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_stream(*args: object, **kwargs: object) -> StreamingStructure:
        calls.append((args, kwargs))
        return StreamingStructure(
            source_locale="en-US",
            target_locale="fr-FR",
            items=(("unit", Data(source="Source", target="Cible")),),
        )

    monkeypatch.setattr(lokit.format_detection, "detect_format", lambda _path: input_format)
    monkeypatch.setattr(lokit.importers, stream_name, fake_stream)

    rows = list(
        lokit.stream.to_dict(
            "input.bin",
            source_language="en-US",
            target_language="fr-FR",
            domain="web",
            fields=(DictField.SOURCE, DictField.TARGET),
        )
    )

    assert rows == [{"source": "Source", "target": "Cible"}]
    assert calls == [(expected_args, expected_kwargs)]


def test_lokit_output_accepts_pot_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "messages.pot"
    calls: list[Path] = []

    def fake_write(_document: object, filepath: str | Path) -> None:
        calls.append(Path(filepath))

    monkeypatch.setattr(lokit.parse.write, "po", fake_write)
    instance = lokit.Lokit.from_document(
        lokit.types.BaseStructure("en", None, {"hello": Data(source="Hello")})
    )

    instance.output(output)

    assert calls == [output]
