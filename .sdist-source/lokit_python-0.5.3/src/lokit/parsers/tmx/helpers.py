from lokit.data.tag_types import TieType as tt

TMX_TAG_MAP: dict[str, tt] = {
    "bpt": tt.CUSTOM_OPEN,
    "ept": tt.CUSTOM_CLOSE,
    "ph": tt.CUSTOM_STANDALONE,
    "it": tt.CUSTOM_STANDALONE,
    "ut": tt.CUSTOM_STANDALONE,
    "x": tt.CUSTOM_STANDALONE,
}
