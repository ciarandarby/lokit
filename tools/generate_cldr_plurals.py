from __future__ import annotations

import hashlib
import pprint
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

CLDR_VERSION = "48.2"
CLDR_TAG = "release-48-2"
BASE_URL = f"https://raw.githubusercontent.com/unicode-org/cldr/{CLDR_TAG}/common/supplemental"
OUTPUT = Path(__file__).parents[1] / "src/lokit/_data/cldr_plural_rules.py"


def _download(name: str) -> bytes:
    request = urllib.request.Request(f"{BASE_URL}/{name}", headers={"User-Agent": "lokit-cldr-generator"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data: object = response.read()
    if not isinstance(data, bytes):
        raise TypeError("CLDR download did not return bytes")
    return data


def _rules(data: bytes) -> dict[str, tuple[tuple[str, str], ...]]:
    root = ET.fromstring(data)
    result: dict[str, tuple[tuple[str, str], ...]] = {}
    for plural_rules in root.findall(".//pluralRules"):
        locales = plural_rules.attrib.get("locales", "").split()
        rules: list[tuple[str, str]] = []
        for plural_rule in plural_rules.findall("pluralRule"):
            category = plural_rule.attrib["count"]
            expression = (plural_rule.text or "").split("@", maxsplit=1)[0].strip()
            if category != "other" and expression:
                rules.append((category, expression))
        value = tuple(rules)
        for locale in locales:
            result[locale.replace("_", "-").lower()] = value
    return dict(sorted(result.items()))


def _render(cardinal_data: bytes, ordinal_data: bytes) -> str:
    cardinal = pprint.pformat(_rules(cardinal_data), width=120, sort_dicts=True)
    ordinal = pprint.pformat(_rules(ordinal_data), width=120, sort_dicts=True)
    cardinal_hash = hashlib.sha256(cardinal_data).hexdigest()
    ordinal_hash = hashlib.sha256(ordinal_data).hexdigest()
    return f'''"""Generated CLDR {CLDR_VERSION} plural rules. Do not edit by hand.

Source data is licensed under Unicode-3.0.
"""

CLDR_VERSION = "{CLDR_VERSION}"
CARDINAL_SOURCE_SHA256 = "{cardinal_hash}"
ORDINAL_SOURCE_SHA256 = "{ordinal_hash}"

CARDINAL_RULES: dict[str, tuple[tuple[str, str], ...]] = {cardinal}

ORDINAL_RULES: dict[str, tuple[tuple[str, str], ...]] = {ordinal}
'''


def main() -> None:
    cardinal_data = _download("plurals.xml")
    ordinal_data = _download("ordinals.xml")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(_render(cardinal_data, ordinal_data), encoding="utf-8")


if __name__ == "__main__":
    main()
