from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import lokit._interchange_rust as interchange_rust
from lokit import parse
from lokit.data.structure import BaseStructure, TranslationStatus
from lokit.parsers.tmx.models import TmxParseMode

if TYPE_CHECKING:
    from pathlib import Path


def _trace_materialize(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    original = interchange_rust.materialize_interchange
    results: list[bool] = []

    def traced(
        path: str,
        format_name: str,
        source_language: str | None = None,
        target_language: str | None = None,
        domain: str | None = None,
        mode: str = "full",
        runtime_placeholders: bool = False,
        inline_placeholders: bool = False,
        syntaxes: list[str] | None = None,
    ) -> BaseStructure | None:
        result = original(
            path,
            format_name,
            source_language,
            target_language,
            domain,
            mode,
            runtime_placeholders,
            inline_placeholders,
            syntaxes,
        )
        results.append(result is not None)
        return result

    monkeypatch.setattr(interchange_rust, "materialize_interchange", traced)
    return results


def _write_simple_tmx(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="fixture" creationtoolversion="2" creationdate="20260816T120000Z"
          segtype="sentence" o-tmf="fixture-memory" adminlang="en-US"
          srclang="en-US" tgtlang="fr-FR" datatype="PlainText">
    <prop type="Client-Name">Acme</prop>
  </header>
  <body>
    <tu tuid="one"><tuv xml:lang="en-US"><seg>Hello</seg></tuv><tuv xml:lang="fr-FR"><seg>Bonjour</seg></tuv></tu>
    <tu tuid="two"><tuv xml:lang="en-US"><seg>Empty</seg></tuv><tuv xml:lang="fr-FR"><seg></seg></tuv></tu>
  </body>
</tmx>
""",
        encoding="utf-8",
    )


def _write_simple_xliff(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="messages" source-language="en-US" target-language="fr-FR" datatype="plaintext">
    <body>
      <trans-unit id="one" xml:space="preserve">
        <source>Hello</source><target state="translated">Bonjour</target>
      </trans-unit>
      <trans-unit id="empty"><source>Empty</source><target state="final"></target></trans-unit>
      <trans-unit id="missing"><source>Missing</source></trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )


def test_simple_tmx_native_materialize_matches_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "simple.tmx"
    _write_simple_tmx(path)
    expected = parse.tmx(str(path), progress=True)
    calls = _trace_materialize(monkeypatch)

    actual = parse.tmx(str(path), progress=False)

    assert calls == [True]
    assert actual == expected
    assert actual.source_locale == "en-US"
    assert actual.target_locale == "fr-FR"
    assert actual.target_locales == ("fr-FR",)
    assert actual.export_origin == "fixture 2"
    assert actual.export_timestamp == "20260816T120000Z"
    assert actual.extensions["property.client_name"] == "Acme"


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        (TmxParseMode.TEXT, TranslationStatus.UNKNOWN),
        (TmxParseMode.TEXT_WITH_STATUS, TranslationStatus.APPROVED),
    ],
)
def test_tmx_native_materialize_preserves_selection_domain_and_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: TmxParseMode,
    expected_status: TranslationStatus,
) -> None:
    path = tmp_path / "selection.tmx"
    path.write_text(
        """<tmx version="1.4"><header srclang="en-US"/><body>
<tu tuid="one"><prop type="x-status">approved</prop>
<tuv xml:lang="en-US"><seg>Hello</seg></tuv>
<tuv xml:lang="fr-FR"><seg>Bonjour</seg></tuv>
<tuv xml:lang="de-DE"><seg>Hallo</seg></tuv></tu>
</body></tmx>""",
        encoding="utf-8",
    )
    expected = parse.tmx(
        str(path),
        "en_US",
        "de_de",
        domain="override",
        mode=mode,
        progress=True,
    )
    calls = _trace_materialize(monkeypatch)

    actual = parse.tmx(
        str(path),
        "en_US",
        "de_de",
        domain="override",
        mode=mode,
        progress=False,
    )

    assert calls == [True]
    assert actual == expected
    assert actual.source_locale == "en-US"
    assert actual.target_locale == "de-DE"
    assert actual.extensions["input_format"] == "tmx"
    assert actual.data["one"].target == "Hallo"
    assert actual.data["one"].targets == {}
    assert actual.data["one"].status is expected_status
    assert actual.data["one"].extensions == {"unit_id": "one", "domain": "override"}


def test_simple_xliff_native_materialize_collapses_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "simple.xliff"
    _write_simple_xliff(path)
    expected = parse.xliff(str(path), progress=True)
    calls = _trace_materialize(monkeypatch)

    actual = parse.xliff(str(path), progress=False)

    assert calls == [True]
    assert actual == expected
    assert actual.format_version == "0.1"
    assert actual.target_locales == ("fr-FR",)
    assert actual.extensions["input_format"] == "xliff"
    assert actual.extensions["xliff_version"] == "1.2"
    assert actual.data["one"].target == "Bonjour"
    assert actual.data["one"].targets == {}
    assert actual.data["one"].extensions["space"] == "preserve"
    assert actual.data["empty"].target == ""
    assert actual.data["empty"].status is TranslationStatus.APPROVED
    assert actual.data["missing"].target is None


def test_complex_documents_use_native_materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmx_path = tmp_path / "rich.tmx"
    tmx_path.write_text(
        """<tmx version="1.4"><header srclang="en-US" tgtlang="fr-FR"/><body>
<tu tuid="rich" usagecount="4"><note>Keep markup</note>
<tuv xml:lang="en-US"><seg>Hello <bpt i="1">&lt;b&gt;</bpt>world<ept i="1">&lt;/b&gt;</ept></seg></tuv>
<tuv xml:lang="fr-FR"><seg>Bonjour <bpt i="1">&lt;b&gt;</bpt>monde<ept i="1">&lt;/b&gt;</ept></seg></tuv></tu>
</body></tmx>""",
        encoding="utf-8",
    )
    xliff_path = tmp_path / "rich.xliff"
    xliff_path.write_text(
        """<xliff version="1.2"><file original="messages" source-language="en-US" target-language="fr-FR"><body>
<trans-unit id="rich"><source>Hello <g id="1">world</g></source>
<target state="final">Bonjour <g id="1">monde</g></target><note>Keep markup</note></trans-unit>
</body></file></xliff>""",
        encoding="utf-8",
    )
    calls = _trace_materialize(monkeypatch)

    tmx = parse.tmx(str(tmx_path), include_tags=True, progress=False)
    xliff = parse.xliff(str(xliff_path), include_tags=True, progress=False)

    assert calls == [True, True]
    assert tmx.data["rich"].tags is not None
    assert tmx.data["rich"].meta.usage_count == 4
    assert tmx.data["rich"].comments[0].context == "Keep markup"
    assert xliff.data["rich"].tags is not None
    assert xliff.data["rich"].comments[0].context == "Keep markup"
    assert xliff.data["rich"].status is TranslationStatus.APPROVED


def test_multitarget_xliff_keeps_completed_native_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "multitarget.xliff"
    units = "".join(
        f'<trans-unit id="u{index}"><source>Source {index}</source><target>Fr {index}</target></trans-unit>'
        for index in range(257)
    )
    path.write_text(
        f"""<xliff version="1.2">
<file original="messages" source-language="en" target-language="fr"><body>{units}</body></file>
<file original="messages" source-language="en" target-language="de"><body>
<trans-unit id="u0"><source>Source 0</source><target>De 0</target></trans-unit>
</body></file></xliff>""",
        encoding="utf-8",
    )
    calls = _trace_materialize(monkeypatch)

    document = parse.xliff(str(path), progress=False)

    assert calls == [True]
    assert len(document.data) == 257
    assert document.target_locale is None
    assert document.target_locales == ("fr", "de")
    assert document.data["u0"].targets["fr"].text == "Fr 0"
    assert document.data["u0"].targets["de"].text == "De 0"


def test_progress_enabled_bypasses_native_materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmx_path = tmp_path / "simple.tmx"
    xliff_path = tmp_path / "simple.xliff"
    _write_simple_tmx(tmx_path)
    _write_simple_xliff(xliff_path)

    def fail_materialize(
        path: str,
        format_name: str,
        source_language: str | None = None,
        target_language: str | None = None,
        domain: str | None = None,
        mode: str = "full",
    ) -> BaseStructure | None:
        raise AssertionError(f"unexpected native materialization for {format_name}: {path}")

    monkeypatch.setattr(interchange_rust, "materialize_interchange", fail_materialize)

    assert len(parse.tmx(str(tmx_path), progress=True).data) == 2
    assert len(parse.xliff(str(xliff_path), progress=True).data) == 3
