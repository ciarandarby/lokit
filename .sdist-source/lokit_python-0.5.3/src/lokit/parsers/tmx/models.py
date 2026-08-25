from dataclasses import dataclass

from lokit.compat import StrEnum


@dataclass
class HeaderData:
    origin: str
    timestamp: str
    srclang: str
    tgtlang: str
    extensions: dict[str, str]


class TmxParseMode(StrEnum):
    FULL = "full"
    TEXT = "text"
    TEXT_WITH_STATUS = "text_status"
