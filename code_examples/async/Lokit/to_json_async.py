import asyncio

import lokit


async def main() -> None:
    await lokit.Lokit.to_jsonl_async("translations.json", "lokit_document.jsonl")


asyncio.run(main())
