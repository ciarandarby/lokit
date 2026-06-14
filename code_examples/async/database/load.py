import asyncio

import lokit


async def main():
    tm = await lokit.database.connect("postgresql://user:password@localhost:5432/translation_memory")
    try:
        streamed = lokit.stream.tmx("translation_memory.tmx")
        stats = await tm.load(streamed)
        print(stats.units_written)
    finally:
        await tm.close()


asyncio.run(main())
