import asyncio

import lokit


async def main() -> None:
    rows = await lokit.parse.async_.to_dict("translations.tmx", target_language="fr")
    print(rows)


asyncio.run(main())
