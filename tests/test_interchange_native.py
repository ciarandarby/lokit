from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lokit.data.structure import TranslationStatus
from lokit.importers import import_tmx, import_xliff

if TYPE_CHECKING:
    from pathlib import Path


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


def test_native_tmx_complex_units_ignore_the_removed_disable_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rich.tmx"
    _write_tmx(source, units=3, rich=True)
    monkeypatch.setenv("LOKIT_DISABLE_RUST_INTERCHANGE", "1")

    document = import_tmx(str(source), "en-US", "fr-FR", progress=False)

    assert list(document.data) == ["u0", "u1", "u2"]
    assert document.data["u0"].source == "Hello 0"
    assert document.data["u0"].target == "Bonjour 0"
    assert document.data["u0"].tags is not None


def test_native_xliff_complex_units_ignore_the_removed_disable_switch(
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

    monkeypatch.setenv("LOKIT_DISABLE_RUST_INTERCHANGE", "1")
    document = import_xliff(str(source), progress=False)

    assert document.data["u1"].source == "Hello world."
    assert document.data["u1"].target == "Bonjour monde."
    assert document.data["u1"].tags is not None


def test_native_reader_streams_xliff_2_segments(tmp_path: Path) -> None:
    source = tmp_path / "version-2.xliff"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:2.0" version="2.1" srcLang="en-US" trgLang="fr-FR">
  <file id="f1" original="app">
    <unit id="u1">
      <notes><note>Keep the product name in English.</note></notes>
      <segment id="s1" state="final">
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
    assert document.data["u1:s1"].status is TranslationStatus.APPROVED
    assert [comment.context for comment in document.data["u1:s1"].comments] == ["Keep the product name in English."]
    assert document.source_locale == "en-US"
    assert document.target_locale == "fr-FR"


def test_native_reader_preserves_tmx_header_children(tmp_path: Path) -> None:
    source = tmp_path / "header-child.tmx"
    source.write_text(
        """<tmx version="1.4">
<header srclang="en" creationtool="Workbench"><prop type="Client Name">Acme</prop></header>
<body><tu tuid="u1"><tuv xml:lang="en"><seg>Source</seg></tuv></tu></body>
</tmx>""",
        encoding="utf-8",
    )

    document = import_tmx(str(source), progress=False)

    assert document.extensions["property.client_name"] == "Acme"


def test_native_reader_accepts_a_dtd_without_resolving_entities(tmp_path: Path) -> None:
    source = tmp_path / "dtd.tmx"
    source.write_text(
        """<?xml version="1.0"?>
<!DOCTYPE tmx [<!ELEMENT tmx ANY>]>
<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="dtd"><tuv xml:lang="en"><seg>Source</seg></tuv></tu>
</body></tmx>""",
        encoding="utf-8",
    )

    document = import_tmx(str(source), progress=False)

    assert document.data["dtd"].source == "Source"


def test_native_reader_rejects_external_entity_resolution(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("must not be parsed", encoding="utf-8")
    source = tmp_path / "external-entity.tmx"
    source.write_text(
        f"""<?xml version="1.0"?>
<!DOCTYPE tmx [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>
<tmx version="1.4"><header srclang="en"/><body>
<tu tuid="xxe"><tuv xml:lang="en"><seg>&xxe;</seg></tuv></tu>
</body></tmx>""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unresolved XML entity reference"):
        import_tmx(str(source), progress=False)


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
