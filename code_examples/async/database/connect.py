import asyncio

import lokit


async def main():
    tm = await lokit.database.connect("postgresql://user:password@localhost:5432/translation_memory")
    await tm.close()


asyncio.run(main())
