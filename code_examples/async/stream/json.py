import asyncio

import lokit


async def main() -> None:
    await lokit.stream.async_.write_jsonl("translations.csv", "translations.jsonl")


asyncio.run(main())
