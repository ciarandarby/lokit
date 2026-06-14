import asyncio

import lokit


async def main():
    tm = await lokit.database.connect("postgresql://user:password@localhost:5432/translation_memory")
    try:
        unit = await tm.unit("u1", source_locale="en-US", target_locale="fr-FR")
        print(unit.source)
    finally:
        await tm.close()


asyncio.run(main())
