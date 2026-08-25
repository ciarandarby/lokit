from __future__ import annotations

import asyncio
from pathlib import Path

import lokit
from lokit.types import BaseStructure, Data, TargetData, TranslationStatus


def build_catalog() -> BaseStructure:
    return BaseStructure(
        source_locale="en-US",
        target_locale="fr-FR",
        target_locales=("fr-FR", "de-DE"),
        data={
            "home.title": Data(
                source="Welcome",
                target="Bienvenue",
                targets={"de-DE": TargetData(text="Willkommen")},
                status=TranslationStatus.TRANSLATED,
            ),
            "home.subtitle": Data(source="A fast, sparse interchange format"),
        },
    )


def synchronous_round_trip(path: Path) -> BaseStructure:
    document = build_catalog()
    lokit.export.lokit(document, path)
    parsed = lokit.parse.lokit(str(path), progress=False)
    if parsed != document:
        raise RuntimeError(".lokit round-trip changed the catalog")
    return parsed


async def asynchronous_streaming_copy(source: Path, output: Path) -> None:
    streamed = lokit.stream.lokit(str(source))
    await lokit.export.async_.lokit(streamed, output)

    unit_ids = [unit_id async for unit_id, _data in lokit.stream.async_.lokit(str(output))]
    if unit_ids != ["home.title", "home.subtitle"]:
        raise RuntimeError("streaming copy changed unit ordering")


async def main() -> None:
    source = Path("messages.lokit")
    copy = Path("messages-copy.lokit")
    synchronous_round_trip(source)
    await asynchronous_streaming_copy(source, copy)


if __name__ == "__main__":
    asyncio.run(main())
