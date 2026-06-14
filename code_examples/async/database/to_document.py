import asyncio

import lokit


async def main():
    tm = await lokit.database.connect("postgresql://user:password@localhost:5432/translation_memory")
    try:
        doc = await tm.to_document(source_locale="en-US", target_locale="fr-FR")
        print(len(doc.data))
    finally:
        await tm.close()


asyncio.run(main())
