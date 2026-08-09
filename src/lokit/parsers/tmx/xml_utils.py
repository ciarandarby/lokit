from __future__ import annotations

from typing import IO, TYPE_CHECKING, Protocol, cast

from lxml import etree

if TYPE_CHECKING:
    from collections.abc import Iterator

    from lxml.etree import _Element


_XML_NAMESPACE_DATA_PREFIX = "lokit:xml-namespaces\n"


class _ElementWithNullablePrefix(Protocol):
    @property
    def prefix(self) -> object: ...


def _element_prefix(element: _Element) -> str:
    value = cast("_ElementWithNullablePrefix", element).prefix
    return value if isinstance(value, str) else ""


def local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    if tag and tag[0] == "{":
        return tag.rsplit("}", 1)[-1]
    return tag


def qualified_name(element: _Element) -> str:
    name = local_name(element.tag)
    prefix = _element_prefix(element)
    return f"{prefix}:{name}" if prefix else name


def xml_namespace_data(element: _Element) -> str:
    required: set[str] = set()
    element_prefix = _element_prefix(element)
    if element_prefix:
        required.add(element_prefix)
    namespaces = element.nsmap
    for raw_attribute in element.attrib:
        attribute = raw_attribute.decode("utf-8") if isinstance(raw_attribute, bytes) else raw_attribute
        if not attribute.startswith("{") or "}" not in attribute:
            continue
        namespace = attribute[1:].split("}", 1)[0]
        for prefix, value in namespaces.items():
            if prefix is not None and value == namespace:
                required.add(prefix)
                break
    if not required:
        return ""
    entries = [
        f"{prefix}={namespace}" for prefix, namespace in namespaces.items() if prefix is not None and prefix in required
    ]
    return _XML_NAMESPACE_DATA_PREFIX + "\n".join(entries)


def is_tag(element: _Element, local: str) -> bool:
    tag = element.tag
    return tag == local or (isinstance(tag, str) and len(tag) > 0 and tag[0] == "{" and tag.endswith("}" + local))


def iterparse_safe(
    source: str | IO[bytes],
    events: tuple[str, ...],
    tag: str | tuple[str, ...] | None = None,
) -> etree.iterparse[etree._Element]:
    return etree.iterparse(
        source,
        events=events,
        tag=tag,
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
    parent = element.getparent()
    if parent is None:
        return
    while element.getprevious() is not None:
        del parent[0]
