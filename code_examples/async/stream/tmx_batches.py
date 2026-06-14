import asyncio

import lokit


async def main():
    async for batch in lokit.stream.async_.tmx_batches("translation_memory.tmx", batch_size=2):
        for unit_id, data in batch:
            print(unit_id, data.source)


asyncio.run(main())
