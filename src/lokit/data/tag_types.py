from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional


class TieType(StrEnum):
    A_OPEN = "a.open"
    A_CLOSE = "a.close"
    ABBR_OPEN = "abbr.open"
    ABBR_CLOSE = "abbr.close"
    B_OPEN = "b.open"
    B_CLOSE = "b.close"
    BDI_OPEN = "bdi.open"
    BDI_CLOSE = "bdi.close"
    BDO_OPEN = "bdo.open"
    BDO_CLOSE = "bdo.close"
    BR = "br.standalone"
    CITE_OPEN = "cite.open"
    CITE_CLOSE = "cite.close"
    CODE_OPEN = "code.open"
    CODE_CLOSE = "code.close"
    DATA_OPEN = "data.open"
    DATA_CLOSE = "data.close"
    DFN_OPEN = "dfn.open"
    DFN_CLOSE = "dfn.close"
    EM_OPEN = "em.open"
    EM_CLOSE = "em.close"
    I_OPEN = "i.open"
    I_CLOSE = "i.close"
    IMG = "img.standalone"
    KBD_OPEN = "kbd.open"
    KBD_CLOSE = "kbd.close"
    MARK_OPEN = "mark.open"
    MARK_CLOSE = "mark.close"
    Q_OPEN = "q.open"
    Q_CLOSE = "q.close"
    RP_OPEN = "rp.open"
    RP_CLOSE = "rp.close"
    RT_OPEN = "rt.open"
    RT_CLOSE = "rt.close"
    RUBY_OPEN = "ruby.open"
    RUBY_CLOSE = "ruby.close"
    S_OPEN = "s.open"
    S_CLOSE = "s.close"
    SAMP_OPEN = "samp.open"
    SAMP_CLOSE = "samp.close"
    SMALL_OPEN = "small.open"
    SMALL_CLOSE = "small.close"
    SPAN_OPEN = "span.open"
    SPAN_CLOSE = "span.close"
    STRONG_OPEN = "strong.open"
    STRONG_CLOSE = "strong.close"
    SUB_OPEN = "sub.open"
    SUB_CLOSE = "sub.close"
    SUP_OPEN = "sup.open"
    SUP_CLOSE = "sup.close"
    TIME_OPEN = "time.open"
    TIME_CLOSE = "time.close"
    U_OPEN = "u.open"
    U_CLOSE = "u.close"
    VAR_OPEN = "var.open"
    VAR_CLOSE = "var.close"
    WBR = "wbr.standalone"
    CUSTOM_OPEN = "custom.open"
    CUSTOM_CLOSE = "custom.close"
    CUSTOM_STANDALONE = "custom.standalone"


@dataclass(slots=True)
class TieData:
    id: str
    type: TieType
    attributes: dict[str, str] = field(default_factory=dict)
    attribute_data: str = ""
    position: int = 0
    order: int = 0
    pair_id: Optional[str] = None
    original_name: Optional[str] = None
    original_text: Optional[str] = None
