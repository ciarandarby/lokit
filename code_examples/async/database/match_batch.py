import asyncio

import lokit


async def main():
    tm = await lokit.database.connect("postgresql://user:password@localhost:5432/translation_memory")
    try:
        matches = await tm.match_batch(
            [
                {"source": "Hello", "source_locale": "en-US", "target_locale": "fr-FR"},
                {"source": "World", "source_locale": "en-US", "target_locale": "fr-FR"},
            ]
        )
        print(len(matches))
    finally:
        await tm.close()


asyncio.run(main())
