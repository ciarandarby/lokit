import asyncio

import lokit


async def main() -> None:
    await lokit.quick_parse.async_.tmx_to_json("translation_memory.tmx", "translations.json")


asyncio.run(main())
