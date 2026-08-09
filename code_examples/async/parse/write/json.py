import asyncio

import lokit
from lokit.types import BaseStructure, Data, TargetData


async def main() -> None:
    doc = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={"u1": Data(source="Hello", targets={"fr-FR": TargetData(text="Bonjour")})},
    )
    await lokit.parse.write.async_.json(doc, "translations.json")


asyncio.run(main())
