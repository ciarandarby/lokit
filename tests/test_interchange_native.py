from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest

from lokit.importers import import_tmx, import_xliff

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    find_spec("lokit._interchange_rust") is None,
    reason="native interchange extension is not built",
)


def test_native_reader_batches_are_bounded(tmp_path: Path) -> None:
    from lokit._interchange_rust import Reader

    source = tmp_path / "bounded.tmx"
    _write_tmx(source, units=7, rich=False)
    reader = Reader(str(source), "tmx", "en-US", "fr-FR")

    first = reader.read_batch(3)
    second = reader.read_batch(3)
    third = reader.read_batch(3)

    assert [len(first), len(second), len(third)] == [3, 3, 1]
    assert reader.read_batch(3) == []
    assert first[0][1] == "u0"
    assert first[0][2] == "Hello 0"
    assert first[0][3] == "Bonjour 0"
    reader.close()
    assert reader.closed


def test_native_tmx_complex_units_match_python_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rich.tmx"
    _write_tmx(source, units=3, rich=True)

    native = import_tmx(str(source), "en-US", "fr-FR", progress=False)
    monkeypatch.setenv("LOKIT_DISABLE_RUST_INTERCHANGE", "1")
    fallback = import_tmx(str(source), "en-US", "fr-FR", progress=False)

    assert native == fallback


def test_native_xliff_complex_units_match_python_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rich.xliff"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff version="1.2" xmlns="urn:oasis:names:tc:xliff:document:1.2">
  <file original="app" source-language="en-US" target-language="fr-FR" datatype="plaintext">
    <body>
      <trans-unit id="u1" xml:space="preserve">
        <source>Hello <g id="1" ctype="bold">world</g>.</source>
        <target state="final">Bonjour <g id="1" ctype="bold">monde</g>.</target>
        <note>Translator note</note>
      </trans-unit>
    </body>
  </file>
</xliff>
""",
        encoding="utf-8",
    )

    native = import_xliff(str(source), progress=False)
    monkeypatch.setenv("LOKIT_DISABLE_RUST_INTERCHANGE", "1")
    fallback = import_xliff(str(source), progress=False)

    assert native == fallback


def test_xliff_2_uses_feature_complete_fallback(tmp_path: Path) -> None:
    source = tmp_path / "version-2.xliff"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:2.0" version="2.1" srcLang="en-US" trgLang="fr-FR">
  <file id="f1" original="app">
    <unit id="u1">
      <segment id="s1">
        <source>Hello <pc id="1" type="fmt:bold">world</pc>.</source>
        <target>Bonjour <pc id="1" type="fmt:bold">monde</pc>.</target>
      </segment>
    </unit>
  </file>
</xliff>
""",
        encoding="utf-8",
    )

    document = import_xliff(str(source), progress=False)

    assert document.data["u1:s1"].source == "Hello world."
    assert document.data["u1:s1"].target == "Bonjour monde."
    assert document.data["u1:s1"].tags is not None


def _write_tmx(path: Path, *, units: int, rich: bool) -> None:
    bodies: list[str] = []
    for index in range(units):
        prop = '<prop type="x-status">translated</prop>' if rich else ""
        source = (
            f'Hello <bpt i="1" type="bold">&lt;b&gt;</bpt>{index}<ept i="1">&lt;/b&gt;</ept>'
            if rich
            else f"Hello {index}"
        )
        target = (
            f'Bonjour <bpt i="1" type="bold">&lt;b&gt;</bpt>{index}<ept i="1">&lt;/b&gt;</ept>'
            if rich
            else f"Bonjour {index}"
        )
        bodies.append(
            f'<tu tuid="u{index}">{prop}'
            f'<tuv xml:lang="en-US"><seg>{source}</seg></tuv>'
            f'<tuv xml:lang="fr-FR"><seg>{target}</seg></tuv>'
            "</tu>"
        )
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="test" creationtoolversion="1" segtype="sentence"
          adminlang="en-US" srclang="en-US" datatype="text"/>
  <body>"""
        + "".join(bodies)
        + """</body>
</tmx>
""",
        encoding="utf-8",
    )
