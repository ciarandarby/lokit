import json
from dataclasses import asdict

from lokit import import_tmx

INPUT_TMX_PATH: str = "en_US-de_DE.tmx"
OUTPUT_JSON_PATH: str = "output/output.json"


def main() -> None:
    base_struct = import_tmx(INPUT_TMX_PATH)

    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(
            asdict(base_struct),
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )


if __name__ == "__main__":
    main()
