import asyncio

import lokit


async def main() -> None:
    async for unit_id, data in lokit.stream.async_.docx("word_doc.docx", source_locale="en-US", target_locale="fr-FR"):
        print(unit_id, data.source)


asyncio.run(main())
