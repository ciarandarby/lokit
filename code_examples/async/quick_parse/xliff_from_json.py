import asyncio

import lokit


async def main():
    await lokit.quick_parse.async_.xliff_from_json("translations.json", "translations.xliff")


asyncio.run(main())
