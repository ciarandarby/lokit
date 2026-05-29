from dataclasses import dataclass


@dataclass
class HeaderData:
    origin: str
    timestamp: str
    srclang: str
    tgtlang: str
    extensions: dict[str, str]
