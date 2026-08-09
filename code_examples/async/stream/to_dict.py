import asyncio

import lokit


async def main() -> None:
    async for row in lokit.stream.async_.to_dict("translations.xliff"):
        print(row)


asyncio.run(main())
