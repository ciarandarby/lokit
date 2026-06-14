from __future__ import annotations

from typing import TYPE_CHECKING

from lokit.exporters.tmx import export_tmx
from lokit.importers import import_tmx, import_tmx_parallel

if TYPE_CHECKING:
    from pathlib import Path


def _write_multitarget_tmx(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="test" creationtoolversion="1" segtype="sentence"
          o-tmf="test" adminlang="en-US" srclang="en-US" datatype="PlainText"/>
  <body>
    <tu tuid="one">
      <tuv xml:lang="en-US"><seg>Hello</seg></tuv>
      <tuv xml:lang="fr-FR"><seg>Bonjour</seg></tuv>
      <tuv xml:lang="de-DE"><seg>Hallo</seg></tuv>
    </tu>
    <tu tuid="regional">
      <tuv xml:lang="en-US"><seg>Color</seg></tuv>
      <tuv xml:lang="en-GB"><seg>Colour</seg></tuv>
      <tuv xml:lang="fr-FR"><seg>Couleur</seg></tuv>
    </tu>
  </body>
</tmx>
""",
        encoding="utf-8",
    )


def test_tmx_imports_multiple_targets_by_default(tmp_path: Path) -> None:
    tmx_file = tmp_path / "multi.tmx"
    _write_multitarget_tmx(tmx_file)

    document = import_tmx(str(tmx_file), source_language="en-US", progress=False)

    assert document.target_locale is None
    assert document.target_locales == ("fr-FR", "de-DE", "en-GB")
    assert document.data["one"].target is None
    assert document.data["one"].targets["fr-FR"].text == "Bonjour"
    assert document.data["one"].targets["de-DE"].text == "Hallo"
    assert document.data["regional"].targets["en-GB"].text == "Colour"


def test_tmx_selected_target_keeps_legacy_shape(tmp_path: Path) -> None:
    tmx_file = tmp_path / "multi.tmx"
    _write_multitarget_tmx(tmx_file)

    document = import_tmx(
        str(tmx_file),
        source_language="en-US",
        target_language="fr-FR",
        progress=False,
    )

    assert document.target_locale == "fr-FR"
    assert document.data["one"].target == "Bonjour"
    assert document.data["one"].targets == {}


def test_tmx_export_roundtrips_multiple_targets(tmp_path: Path) -> None:
    source = tmp_path / "multi.tmx"
    exported = tmp_path / "exported.tmx"
    _write_multitarget_tmx(source)

    document = import_tmx(str(source), source_language="en-US", progress=False)
    export_tmx(document, exported)
    roundtripped = import_tmx(str(exported), source_language="en-US", progress=False)

    assert roundtripped.target_locales == ("fr-FR", "de-DE", "en-GB")
    assert roundtripped.data["one"].targets["fr-FR"].text == "Bonjour"
    assert roundtripped.data["regional"].targets["en-GB"].text == "Colour"


def test_tmx_parallel_matches_multitarget_semantics(tmp_path: Path) -> None:
    tmx_file = tmp_path / "multi.tmx"
    _write_multitarget_tmx(tmx_file)

    document = import_tmx_parallel(str(tmx_file), source_language="en-US", progress=False)

    assert document.data["one"].targets["fr-FR"].text == "Bonjour"
    assert document.data["one"].targets["de-DE"].text == "Hallo"
