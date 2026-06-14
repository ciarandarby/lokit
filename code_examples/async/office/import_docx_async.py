import asyncio

import lokit


async def main():
    async for item in lokit.office.import_docx_async("word_doc.docx", source_locale="en-US", target_locale="fr-FR"):
        print(item)


asyncio.run(main())
