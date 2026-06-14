import asyncio

import lokit


async def main():
    doc = lokit.BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={"u1": lokit.Data(source="Hello", targets={"fr-FR": lokit.TargetData(text="Bonjour")})},
    )
    await lokit.parse.write.async_.docx(doc, "translated_word_doc.docx", source_docx="word_doc.docx")


asyncio.run(main())
