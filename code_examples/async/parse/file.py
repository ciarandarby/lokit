import asyncio

import lokit


async def main():
    async for unit_id, data in lokit.parse.async_.file("translations.json"):
        print(unit_id, data.source)


asyncio.run(main())
