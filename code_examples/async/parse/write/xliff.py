import asyncio

import lokit
from lokit.types import BaseStructure, Data, TargetData


async def main() -> None:
    doc = BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        data={
            "u1": Data(
                source="Hello",
                targets={"fr-FR": TargetData(text="Bonjour")},
                extensions={"resource": "messages.json"},
            )
        },
    )
    await lokit.parse.write.async_.xliff(doc, "translations.xliff", group_by_resource=True)


asyncio.run(main())
