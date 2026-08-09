from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

import pytest
from lxml import etree

import lokit
from lokit.data.structure import Data, StreamingStructure, TargetData

if TYPE_CHECKING:
    from pathlib import Path


ExtractItem = tuple[str, Data]


class _FailingClosableItems:
    def __init__(self) -> None:
        self._index = 0
        self.closed = False

    def __iter__(self) -> _FailingClosableItems:
        return self

    def __next__(self) -> ExtractItem:
        if self._index == 0:
            self._index = 1
            return "before-error", Data(source="Source", targets={"fr": TargetData(text="Cible")})
        raise RuntimeError("intentional producer failure")

    def close(self) -> None:
        self.closed = True


class _SlowClosableItems:
    def __init__(self) -> None:
        self._index = 0
        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self) -> _SlowClosableItems:
        return self

    def __next__(self) -> ExtractItem:
        if self.closed.is_set() or self._index >= 100:
            raise StopIteration
        self.started.set()
        time.sleep(0.01)
        index = self._index
        self._index += 1
        return (
            f"unit-{index}",
            Data(source=f"Source {index}", targets={"fr": TargetData(text=f"Cible {index}")}),
        )

    def close(self) -> None:
        self.closed.set()


class _MultilingualItems:
    def __init__(self) -> None:
        self._consumed = False
        self.closed = False

    def __iter__(self) -> _MultilingualItems:
        return self

    def __next__(self) -> ExtractItem:
        if self._consumed:
            raise StopIteration
        self._consumed = True
        return (
            "welcome",
            Data(
                source="Hello",
                targets={
                    "fr": TargetData(text="Bonjour"),
                    "de": TargetData(text="Hallo"),
                },
            ),
        )

    def close(self) -> None:
        self.closed = True


def test_failed_lokit_export_closes_caller_iterator(tmp_path: Path) -> None:
    items = _FailingClosableItems()
    output = tmp_path / "failed.lokit"
    output.write_bytes(b"existing\n")
    document = StreamingStructure("en", None, items)

    with pytest.raises(RuntimeError, match="intentional producer failure"):
        lokit.export.lokit(document, output)

    assert items.closed
    assert output.read_bytes() == b"existing\n"
    assert list(tmp_path.glob(".failed.lokit.*.tmp")) == []


@pytest.mark.asyncio
async def test_cancelled_lokit_export_closes_caller_iterator(tmp_path: Path) -> None:
    items = _SlowClosableItems()
    output = tmp_path / "cancelled.lokit"
    output.write_bytes(b"existing\n")
    document = StreamingStructure("en", None, items)
    task = asyncio.create_task(lokit.export.async_.lokit(document, output))
    assert await asyncio.to_thread(items.started.wait, 2.0)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert items.closed.is_set()
    assert output.read_bytes() == b"existing\n"
    assert list(tmp_path.glob(".cancelled.lokit.*.tmp")) == []


def test_failed_direct_streaming_split_closes_caller_iterator() -> None:
    items = _FailingClosableItems()
    context = StreamingStructure("en", None, items).split_targets(("fr",))

    with pytest.raises(RuntimeError, match="intentional producer failure"), context:
        pass

    assert items.closed


def test_streaming_multilingual_xliff_discovers_and_writes_every_target(tmp_path: Path) -> None:
    items = _MultilingualItems()
    output = tmp_path / "multilingual.xliff"
    document = StreamingStructure("en", None, items)

    lokit.export.xliff(document, output)

    assert items.closed
    namespace = {"x": "urn:oasis:names:tc:xliff:document:1.2"}
    files = etree.parse(str(output)).getroot().findall("x:file", namespace)
    assert [file_element.attrib["target-language"] for file_element in files] == ["fr", "de"]
    assert [file_element.findtext("x:body/x:trans-unit/x:target", namespaces=namespace) for file_element in files] == [
        "Bonjour",
        "Hallo",
    ]
    assert lokit.parse.to_dict(
        output,
        fields=(lokit.types.DictField.TARGET_LOCALE, lokit.types.DictField.TARGET),
    ) == [
        {"target_locale": "fr", "target": "Bonjour"},
        {"target_locale": "de", "target": "Hallo"},
    ]


def test_streaming_source_only_xliff_survives_discovery_spool(tmp_path: Path) -> None:
    output = tmp_path / "source-only.xliff"
    document = StreamingStructure("en", None, iter((("source", Data(source="Only source")),)))

    lokit.export.xliff(document, output)

    namespace = {"x": "urn:oasis:names:tc:xliff:document:1.2"}
    file_element = etree.parse(str(output)).getroot().find("x:file", namespace)
    assert file_element is not None
    assert "target-language" not in file_element.attrib
    assert file_element.findtext("x:body/x:trans-unit/x:source", namespaces=namespace) == "Only source"
    assert file_element.find("x:body/x:trans-unit/x:target", namespace) is None


@pytest.mark.asyncio
async def test_cancelled_multilingual_xliff_spool_closes_caller_iterator(tmp_path: Path) -> None:
    items = _SlowClosableItems()
    output = tmp_path / "cancelled-multilingual.xliff"
    output.write_bytes(b"existing\n")
    document = StreamingStructure("en", None, items)
    task = asyncio.create_task(lokit.export.async_.xliff(document, output))
    assert await asyncio.to_thread(items.started.wait, 2.0)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert items.closed.is_set()
    assert output.read_bytes() == b"existing\n"
    assert list(tmp_path.glob(".cancelled-multilingual.xliff.*.tmp")) == []
