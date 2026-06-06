from dataclasses import dataclass
from typing import Optional

from lxml.etree import _Attrib, _Element

from lokit._accelerators import STATUS_CODE
from lokit.data.structure import AdjacentContext, Comment, Meta, Origin, TranslationStatus
from lokit.parsers.tmx.xml_utils import element_children, local_name


@dataclass(slots=True)
class ParsedTmxProps:
    meta: Meta
    comments: list[Comment]
    previous_context: Optional[AdjacentContext]
    next_context: Optional[AdjacentContext]
    status: TranslationStatus
    extensions: dict[str, str]


class TmxProps:
    def __init__(self) -> None:
        self._known_props: frozenset[str] = frozenset(
            {
                "status",
                "x-status",
                "x-project",
                "x-system",
                "x-domain",
                "x-context",
                "x-key",
                "note",
                "x-note",
                "comment",
                "x-comment",
                "x-previous-id",
                "x-previous-source",
                "x-previous-source-text",
                "x-previous-target",
                "x-previous-target-text",
                "x-next-id",
                "x-next-source",
                "x-next-source-text",
                "x-next-target",
                "x-next-target-text",
            }
        )

    def parse_all(self, element: _Element) -> ParsedTmxProps:
        attrs = element.attrib
        change_date = attrs.get("changedate")
        creator = attrs.get("creationid") or ""
        project = ""
        system = ""
        context_key = ""
        comments: list[Comment] = []
        status_values: list[str] = []
        extensions: dict[str, str] = {}
        prev_id: str | None = None
        prev_src: str | None = None
        prev_tgt: str | None = None
        next_id: str | None = None
        next_src: str | None = None
        next_tgt: str | None = None
        has_metadata_children = False

        for child in element:
            tag_name = local_name(child.tag)
            if tag_name == "note" and child.text:
                has_metadata_children = True
                comments.append(Comment(context=child.text.strip(), timestamp=change_date))
                continue
            if tag_name != "prop":
                continue
            has_metadata_children = True

            prop_type = child.attrib.get("type", "").lower()
            text_val = child.text or ""
            if self.is_status_prop(prop_type):
                status_values.append(text_val.strip().lower())
            elif prop_type == "x-project":
                project = text_val
            elif prop_type in ("x-system", "x-domain"):
                system = text_val
            elif prop_type in ("x-context", "x-key"):
                context_key = text_val
            elif prop_type in ("note", "x-note", "comment", "x-comment"):
                comments.append(Comment(context=text_val.strip(), timestamp=change_date))
            elif prop_type == "x-previous-id":
                prev_id = text_val
            elif prop_type in ("x-previous-source", "x-previous-source-text"):
                prev_src = text_val
            elif prop_type in ("x-previous-target", "x-previous-target-text"):
                prev_tgt = text_val
            elif prop_type == "x-next-id":
                next_id = text_val
            elif prop_type in ("x-next-source", "x-next-source-text"):
                next_src = text_val
            elif prop_type in ("x-next-target", "x-next-target-text"):
                next_tgt = text_val
            elif prop_type not in self._known_props:
                extensions[f"property.{self._normalize_key(prop_type or 'unknown')}"] = text_val

        origin = Origin(
            system=system if system else None,
            project=project if project else None,
            creator_id=creator if creator else None,
        )
        if any([project, system, creator, context_key]) and not comments:
            comments.append(Comment(context=""))
        for comment in comments:
            comment.origin = origin if any([system, project, creator]) else None
            comment.context_key = context_key if context_key else None
            comment.timestamp = comment.timestamp or change_date

        usage_count_raw = attrs.get("usagecount") or ""
        usage_count = int(usage_count_raw) if usage_count_raw.isdigit() else None
        meta_extensions = self.parse_meta_extensions_from_attrs(attrs)
        has_meta = (
            usage_count is not None
            or attrs.get("lastusagedate") is not None
            or attrs.get("creationdate") is not None
            or change_date is not None
            or bool(meta_extensions)
        )
        if not has_metadata_children and not has_meta:
            return ParsedTmxProps(
                meta=Meta(),
                comments=[],
                previous_context=None,
                next_context=None,
                status=TranslationStatus.UNKNOWN,
                extensions={},
            )
        meta = Meta(
            usage_count=usage_count,
            last_used=attrs.get("lastusagedate"),
            first_used=None,
            created=attrs.get("creationdate") or None,
            updated=change_date,
            max_length=None,
            min_length=None,
            extensions=meta_extensions,
        )
        prev_ctx = (
            AdjacentContext(unit_id=prev_id, source=prev_src, target=prev_tgt)
            if any([prev_id, prev_src, prev_tgt])
            else None
        )
        next_ctx = (
            AdjacentContext(unit_id=next_id, source=next_src, target=next_tgt)
            if any([next_id, next_src, next_tgt])
            else None
        )
        return ParsedTmxProps(
            meta=meta,
            comments=comments,
            previous_context=prev_ctx,
            next_context=next_ctx,
            status=self.status_from_values(status_values),
            extensions=extensions,
        )

    def parse_meta(self, element: _Element) -> Meta:
        creation_date: str = element.attrib.get("creationdate") or ""
        _usage_count: str = element.attrib.get("usagecount") or ""
        usage_count: int | None = (
            int(_usage_count) if _usage_count.isdigit() else None
        )
        return Meta(
            usage_count=usage_count,
            last_used=element.attrib.get("lastusagedate"),
            first_used=None,
            created=creation_date if creation_date else None,
            updated=element.attrib.get("changedate"),
            max_length=None,
            min_length=None,
            extensions=self.parse_meta_extensions(element),
        )

    def parse_comments(self, element: _Element) -> list[Comment]:
        project: str = ""
        system: str = ""
        creator: str = element.attrib.get("creationid") or ""
        context_key: str = ""
        comments: list[Comment] = []

        for child in element_children(element):
            tag_name: str = local_name(child.tag)
            if tag_name == "prop":
                prop_type: str = child.attrib.get("type", "").lower()
                text_val: str = child.text or ""
                if prop_type == "x-project":
                    project = text_val
                elif prop_type in ("x-system", "x-domain"):
                    system = text_val
                elif prop_type in ("x-context", "x-key"):
                    context_key = text_val
                elif prop_type in ("note", "x-note", "comment", "x-comment"):
                    comments.append(
                        Comment(
                            context=text_val.strip(),
                            timestamp=element.attrib.get("changedate"),
                        )
                    )

            elif tag_name == "note" and child.text:
                comments.append(
                    Comment(
                        context=child.text.strip(),
                        timestamp=element.attrib.get("changedate"),
                    )
                )

        if not any([project, system, creator, context_key]) and not comments:
            return []

        origin: Origin = Origin(
            system=system if system else None,
            project=project if project else None,
            creator_id=creator if creator else None,
        )

        if not comments:
            comments.append(Comment(context=""))

        for comment in comments:
            comment.origin = origin if any([system, project, creator]) else None
            comment.context_key = context_key if context_key else None
            comment.timestamp = comment.timestamp or element.attrib.get("changedate")

        return comments

    def parse_status(self, element: _Element) -> TranslationStatus:
        status_values: list[str] = []

        for child in element_children(element, "prop"):
            prop_type: str = child.attrib.get("type", "").lower()
            if self.is_status_prop(prop_type):
                status_values.append((child.text or "").strip().lower())

        for value in reversed(status_values):
            parsed = self.status_from_values([value])
            if parsed != TranslationStatus.UNKNOWN:
                return parsed

        return TranslationStatus.UNKNOWN

    def parse_adjacent_context(
        self, element: _Element
    ) -> tuple[AdjacentContext | None, AdjacentContext | None]:
        prev_id: str | None = None
        prev_src: str | None = None
        prev_tgt: str | None = None
        next_id: str | None = None
        next_src: str | None = None
        next_tgt: str | None = None

        for child in element_children(element, "prop"):
            prop_type: str = child.attrib.get("type", "").lower()
            text_val: str = child.text or ""
            if prop_type == "x-previous-id":
                prev_id = text_val
            elif prop_type in ("x-previous-source", "x-previous-source-text"):
                prev_src = text_val
            elif prop_type in ("x-previous-target", "x-previous-target-text"):
                prev_tgt = text_val
            elif prop_type == "x-next-id":
                next_id = text_val
            elif prop_type in ("x-next-source", "x-next-source-text"):
                next_src = text_val
            elif prop_type in ("x-next-target", "x-next-target-text"):
                next_tgt = text_val

        prev_ctx: AdjacentContext | None = (
            AdjacentContext(unit_id=prev_id, source=prev_src, target=prev_tgt)
            if any([prev_id, prev_src, prev_tgt])
            else None
        )

        next_ctx: AdjacentContext | None = (
            AdjacentContext(
                unit_id=next_id,
                source=next_src,
                target=next_tgt,
            )
            if any([next_id, next_src, next_tgt])
            else None
        )

        return prev_ctx, next_ctx

    def parse_meta_extensions(self, element: _Element) -> dict[str, str]:
        return self.parse_meta_extensions_from_attrs(element.attrib)

    def parse_meta_extensions_from_attrs(self, attrs: _Attrib) -> dict[str, str]:
        extensions: dict[str, str] = {}

        change_id = attrs.get("changeid")
        if change_id:
            extensions["change_id"] = change_id

        usage_count = attrs.get("usagecount")
        if usage_count:
            extensions["usage_count_raw"] = usage_count

        return extensions

    def parse_extensions(self, element: _Element) -> dict[str, str]:
        extensions: dict[str, str] = {}

        for child in element_children(element, "prop"):
            prop_type = (child.attrib.get("type") or "unknown").lower()
            if prop_type in self._known_props or self.is_status_prop(prop_type):
                continue
            extensions[f"property.{self._normalize_key(prop_type)}"] = child.text or ""

        return extensions

    def is_status_prop(self, prop_type: str) -> bool:
        return prop_type in ("status", "x-status") or (
            prop_type.startswith("x-") and prop_type.endswith("-status")
        )

    def _normalize_key(self, value: str) -> str:
        return value.lower().replace(" ", "_").replace("-", "_")

    def status_from_values(self, values: list[str]) -> TranslationStatus:
        for value in reversed(values):
            status_code = STATUS_CODE(value)
            if status_code == 1:
                return TranslationStatus.APPROVED
            if status_code == 2:
                return TranslationStatus.REVIEWED
            if status_code == 3:
                return TranslationStatus.TRANSLATED
            if status_code == 4:
                return TranslationStatus.NEW
            if status_code == 5:
                return TranslationStatus.DRAFT
            if status_code == 6:
                return TranslationStatus.REJECTED
        return TranslationStatus.UNKNOWN
