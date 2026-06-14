from __future__ import annotations

from typing import TYPE_CHECKING, BinaryIO

from lxml import etree

if TYPE_CHECKING:
    from collections.abc import Iterator

    from lxml.etree import _Element


def local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    if tag and tag[0] == "{":
        return tag.rsplit("}", 1)[-1]
    return tag


def is_tag(element: _Element, local: str) -> bool:
    tag = element.tag
    return tag == local or (isinstance(tag, str) and len(tag) > 0 and tag[0] == "{" and tag.endswith("}" + local))


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
