from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import lokit.exporters.html as html_exporter
import lokit.exporters.idml as idml_exporter
import lokit.exporters.json_i18n as json_exporter
import lokit.exporters.po as po_exporter
from lokit.data.structure import BaseStructure
from lokit.io.filenames import (
    FILENAME_COLLISION,
    FILENAME_TOO_LONG,
    TOO_MANY_OUTPUTS,
    LocaleFilenameError,
    locale_output_names,
)
from lokit.office.backend import _target_output_names as office_target_output_names
from lokit.office.errors import OfficeReinsertionError

if TYPE_CHECKING:
    from pathlib import Path


def test_locale_output_names_are_normalized_and_exact_duplicates_are_ignored() -> None:
    locale = "e\N{COMBINING ACUTE ACCENT}"

    names = locale_output_names((locale, locale, "sr_RS@latin"), suffix=".po")

    assert names == ((locale, "é.po"), ("sr_RS@latin", "sr_RS@latin.po"))


@pytest.mark.parametrize(
    "locales",
    [
        ("fr", "FR"),
        ("é", "e\N{COMBINING ACUTE ACCENT}"),
        ("ß", "ss"),
    ],
)
def test_locale_output_names_reject_case_and_unicode_collisions(locales: tuple[str, ...]) -> None:
    with pytest.raises(LocaleFilenameError) as captured:
        locale_output_names(locales, suffix=".json")

    assert captured.value.reason == FILENAME_COLLISION


@pytest.mark.parametrize(
    "locale",
    [
        ".fr",
        "../fr",
        "fr/de",
        "fr\\de",
        "fr:de",
        "fr\N{RIGHT-TO-LEFT OVERRIDE}",
        "fr\n",
        "fr ",
        "CON",
        "con.anything",
        "COM\N{SUPERSCRIPT ONE}",
        "LPT\N{SUPERSCRIPT THREE}.regional",
        "CONIN$",
    ],
)
def test_locale_output_names_reject_nonportable_components(locale: str) -> None:
    with pytest.raises(LocaleFilenameError):
        locale_output_names((locale,), suffix=".idml")


def test_locale_output_names_check_complete_component_and_decomposed_size() -> None:
    assert locale_output_names(("a" * 252,), suffix=".po") == (("a" * 252, f"{'a' * 252}.po"),)
    assert locale_output_names(("é" * 84,), suffix=".po") == (("é" * 84, f"{'é' * 84}.po"),)

    with pytest.raises(LocaleFilenameError) as byte_error:
        locale_output_names(("a" * 253,), suffix=".po")
    assert byte_error.value.reason == FILENAME_TOO_LONG

    with pytest.raises(LocaleFilenameError) as normalization_error:
        locale_output_names(("é" * 85,), suffix=".po")
    assert normalization_error.value.reason == FILENAME_TOO_LONG


def test_locale_output_names_bound_requested_output_count() -> None:
    locales = tuple(f"locale-{index}" for index in range(257))

    with pytest.raises(LocaleFilenameError) as captured:
        locale_output_names(locales, suffix=".json")

    assert captured.value.reason == TOO_MANY_OUTPUTS

    with pytest.raises(LocaleFilenameError) as duplicate_error:
        locale_output_names(("fr",) * 257, suffix=".json")
    assert duplicate_error.value.reason == TOO_MANY_OUTPUTS


def test_text_exporters_share_portable_locale_filename_policy() -> None:
    locale = "e\N{COMBINING ACUTE ACCENT}"

    assert po_exporter._target_output_names((locale,)) == {locale: "é.po"}
    assert json_exporter._locale_output_names((locale,)) == {locale: "é.json"}
    assert html_exporter._locale_output_names((locale,)) == {locale: "index.é.html"}

    for output_names in (
        po_exporter._target_output_names,
        json_exporter._locale_output_names,
        html_exporter._locale_output_names,
    ):
        with pytest.raises(ValueError, match=r"[Cc]olliding"):
            output_names(("é", locale))
        with pytest.raises(ValueError, match="Unsafe"):
            output_names(("CON",))


def test_office_shares_portable_bounded_locale_filename_policy() -> None:
    locale = "e\N{COMBINING ACUTE ACCENT}"

    assert office_target_output_names((locale,), "docx") == ((locale, "é.docx"),)

    for unsafe_locale in (".fr", "fr\N{RIGHT-TO-LEFT OVERRIDE}", "CONIN$", "COM\N{SUPERSCRIPT ONE}"):
        with pytest.raises(OfficeReinsertionError):
            office_target_output_names((unsafe_locale,), "docx")

    with pytest.raises(OfficeReinsertionError, match="at most 256"):
        office_target_output_names(tuple(f"locale-{index}" for index in range(257)), "pptx")


def test_exporter_adapters_bound_raw_duplicate_locale_requests(tmp_path: Path) -> None:
    repeated = ("fr",) * 257
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=repeated,
        data={},
    )

    with pytest.raises(ValueError, match="at most 256"):
        po_exporter._target_output_names(repeated)
    with pytest.raises(ValueError, match="at most 256"):
        html_exporter._locale_output_names(repeated)
    with pytest.raises(ValueError, match="at most 256"):
        json_exporter._document_target_locales(document)
    with pytest.raises(ValueError, match="at most 256"):
        idml_exporter._resolve_outputs(document, tmp_path / "idml")
    with pytest.raises(OfficeReinsertionError, match="at most 256"):
        office_target_output_names(repeated, "docx")


def test_idml_uses_portable_normalized_locale_output_names(tmp_path: Path) -> None:
    locale = "e\N{COMBINING ACUTE ACCENT}"
    document = BaseStructure(
        source_locale="en",
        target_locale=None,
        target_locales=(locale,),
        data={},
    )

    assert idml_exporter._resolve_outputs(document, tmp_path / "outputs") == (
        (locale, tmp_path / "outputs" / "é.idml"),
    )

    document.target_locales = ("é", locale)
    with pytest.raises(ValueError, match="colliding"):
        idml_exporter._resolve_outputs(document, tmp_path / "outputs")

    document.target_locales = ("NUL",)
    with pytest.raises(ValueError, match="unsafe IDML target locale"):
        idml_exporter._resolve_outputs(document, tmp_path / "outputs")
