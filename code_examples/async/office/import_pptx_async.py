import asyncio

import lokit


async def main():
    async for item in lokit.office.import_pptx_async("presentation.pptx", source_locale="en-US", target_locale="fr-FR"):
        print(item)


asyncio.run(main())
