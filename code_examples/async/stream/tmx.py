import asyncio

import lokit
from lokit.types import TagSyntax


async def main() -> None:
    async for unit_id, data in lokit.stream.async_.tmx(
        "translation_memory.tmx",
        include_tags=True,
        tag_syntax=TagSyntax.HTML,
    ):
        print(unit_id, data.source)


asyncio.run(main())
