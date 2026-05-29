from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO

from lxml import etree
from lxml.etree import _Element


def local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def iterparse_safe(
    source: str | BinaryIO,
    events: tuple[str, ...],
) -> etree.iterparse[etree._Element]:
    return etree.iterparse(
        source,
        events=events,
        no_network=True,
        resolve_entities=False,
    )


def element_children(element: _Element, name: str | None = None) -> Iterator[_Element]:
    for child in element:
        if name is None or local_name(child.tag) == name:
            yield child


def find_child(element: _Element, name: str) -> _Element | None:
    for child in element_children(element, name):
        return child
    return None


def clear_element(element: _Element) -> None:
    element.clear()
    while element.getprevious() is not None:
        parent = element.getparent()
        if parent is None:
            break
        del parent[0]
