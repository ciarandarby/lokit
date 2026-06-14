import asyncio

import lokit


async def main():
    doc = lokit.office.import_pptx("presentation.pptx", source_locale="en-US", target_locale="fr-FR")
    await lokit.office.export_pptx_async(doc, "translated_presentation.pptx", source_pptx="presentation.pptx")


asyncio.run(main())
