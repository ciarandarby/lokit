import asyncio

import lokit


async def main():
    await lokit.stream.async_.json("translations.csv", "translations.json")


asyncio.run(main())
