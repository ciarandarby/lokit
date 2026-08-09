import lokit
from lokit.types import DictField, StringMode

rows = lokit.stream.to_dict(
    "translations.tmx",
    strings=StringMode.RAW,
    fields=(
        DictField.UNIT_ID,
        DictField.SOURCE_LANGUAGE,
        DictField.TARGET_LANGUAGE,
        DictField.SOURCE,
        DictField.TARGET,
        DictField.DOMAIN,
    ),
)

for row in rows:
    print(row)
