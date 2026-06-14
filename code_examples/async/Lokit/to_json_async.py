import asyncio

import lokit


async def main():
    await lokit.Lokit.to_json_async("translations.json", "lokit_document.json")


asyncio.run(main())
