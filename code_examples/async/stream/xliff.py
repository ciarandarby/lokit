import asyncio

import lokit


async def main():
    async for unit_id, data in lokit.stream.async_.xliff("translations.xliff"):
        print(unit_id, data.source)


asyncio.run(main())
