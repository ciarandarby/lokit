import asyncio

import lokit


async def main() -> None:
    async for unit_id, data in lokit.parse.async_.pptx(
        "presentation.pptx", source_locale="en-US", target_locale="fr-FR"
    ):
        print(unit_id, data.source)


asyncio.run(main())
