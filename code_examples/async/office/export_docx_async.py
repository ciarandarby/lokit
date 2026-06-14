import asyncio

import lokit


async def main():
    doc = lokit.office.import_docx("word_doc.docx", source_locale="en-US", target_locale="fr-FR")
    await lokit.office.export_docx_async(doc, "translated_word_doc.docx", source_docx="word_doc.docx")


asyncio.run(main())
