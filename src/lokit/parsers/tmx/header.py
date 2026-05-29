from lxml.etree import _Element

from lokit.parsers.tmx.models import HeaderData
from lokit.parsers.tmx.xml_utils import element_children, local_name


class TmxHeaderParser:
    def __init__(self) -> None:
        pass

    def parse(self, element: _Element) -> HeaderData:
        creation_tool: str = element.attrib.get("creationtool") or "unknown_origin"
        tool_version: str = element.attrib.get("creationtoolversion") or ""
        origin: str = f"{creation_tool} {tool_version}".strip()
        timestamp: str = element.attrib.get("creationdate") or ""
        extensions: dict[str, str] = {
            "input_format": "tmx",
        }
        self._add_if_present(extensions, "admin_locale", element.attrib.get("adminlang"))
        self._add_if_present(extensions, "data_type", element.attrib.get("datatype"))
        self._add_if_present(extensions, "segmentation", element.attrib.get("segtype"))
        self._add_if_present(extensions, "translation_memory_format", element.attrib.get("o-tmf"))
        self._add_if_present(extensions, "tool_name", creation_tool)
        self._add_if_present(extensions, "tool_version", tool_version)

        srclang: str = element.attrib.get("srclang") or ""
        if srclang == "*all*":
            srclang = ""

        tgtlang: str = element.attrib.get("tgtlang") or ""

        for child in element_children(element):
            child_name = local_name(child.tag)
            if child_name == "prop":
                prop_type = child.attrib.get("type") or "unknown"
                extensions[f"property.{self._normalize_key(prop_type)}"] = child.text or ""
            elif child.text:
                extensions[f"property.{self._normalize_key(child_name)}"] = child.text

        return HeaderData(
            origin=origin,
            timestamp=timestamp,
            srclang=srclang,
            tgtlang=tgtlang,
            extensions=extensions,
        )

    def _add_if_present(
        self, extensions: dict[str, str], key: str, value: str | None
    ) -> None:
        if value:
            extensions[key] = value

    def _normalize_key(self, value: str) -> str:
        return value.lower().replace(" ", "_").replace("-", "_")
