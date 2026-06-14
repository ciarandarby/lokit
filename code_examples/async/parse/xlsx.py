import asyncio

import lokit


async def main():
    async for unit_id, data in lokit.parse.async_.xlsx(
        "translations.xlsx", source_locale="en-US", target_locale="fr-FR"
    ):
        print(unit_id, data.source)


asyncio.run(main())
