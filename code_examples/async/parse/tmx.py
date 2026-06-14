import asyncio

import lokit


async def main():
    async for unit_id, data in lokit.parse.async_.tmx(
        "translation_memory.tmx", source_language="en", target_language="fr"
    ):
        print(unit_id, data.source)


asyncio.run(main())
