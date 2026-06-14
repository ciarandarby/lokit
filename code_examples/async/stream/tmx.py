import asyncio

import lokit


async def main():
    async for unit_id, data in lokit.stream.async_.tmx("translation_memory.tmx"):
        print(unit_id, data.source)


asyncio.run(main())
