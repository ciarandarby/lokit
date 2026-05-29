from typing import Optional

from lxml.etree import _Element

from lokit.data.structure import AdjacentContext, Comment, Meta, Origin, TranslationStatus
from lokit.parsers.tmx.xml_utils import element_children, local_name


class TmxProps:
    def __init__(self) -> None:
        self._known_props: frozenset[str] = frozenset(
            {
                "status",
                "x-status",
                "x-xtm-status",
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

    def parse_meta(self, element: _Element) -> Meta:
        creation_date: str = element.attrib.get("creationdate") or ""
        _usage_count: str = element.attrib.get("usagecount") or ""
        usage_count: Optional[int] = (
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
            if prop_type in ("status", "x-status", "x-xtm-status"):
                status_values.append((child.text or "").strip().lower())

        for value in reversed(status_values):
            if value in ("approved", "signed-off", "final"):
                return TranslationStatus.APPROVED
            if value in ("reviewed", "review"):
                return TranslationStatus.REVIEWED
            if value in ("translated", "complete"):
                return TranslationStatus.TRANSLATED
            if value in ("new",):
                return TranslationStatus.NEW
            if value in ("draft", "notapproved", "not-approved", "unapproved"):
                return TranslationStatus.DRAFT
            if value in ("rejected",):
                return TranslationStatus.REJECTED

        return TranslationStatus.UNKNOWN

    def parse_adjacent_context(
        self, element: _Element
    ) -> tuple[Optional[AdjacentContext], Optional[AdjacentContext]]:
        prev_id: Optional[str] = None
        prev_src: Optional[str] = None
        prev_tgt: Optional[str] = None
        next_id: Optional[str] = None
        next_src: Optional[str] = None
        next_tgt: Optional[str] = None

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

        prev_ctx: Optional[AdjacentContext] = (
            AdjacentContext(unit_id=prev_id, source=prev_src, target=prev_tgt)
            if any([prev_id, prev_src, prev_tgt])
            else None
        )

        next_ctx: Optional[AdjacentContext] = (
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
        extensions: dict[str, str] = {}

        change_id = element.attrib.get("changeid")
        if change_id:
            extensions["change_id"] = change_id

        usage_count = element.attrib.get("usagecount")
        if usage_count:
            extensions["usage_count_raw"] = usage_count

        return extensions

    def parse_extensions(self, element: _Element) -> dict[str, str]:
        extensions: dict[str, str] = {}

        for child in element_children(element, "prop"):
            prop_type = (child.attrib.get("type") or "unknown").lower()
            if prop_type in self._known_props:
                continue
            extensions[f"property.{self._normalize_key(prop_type)}"] = child.text or ""

        return extensions

    def _normalize_key(self, value: str) -> str:
        return value.lower().replace(" ", "_").replace("-", "_")
