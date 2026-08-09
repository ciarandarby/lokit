import lokit
from lokit.types import BaseStructure, Data

doc = BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": Data(source="Hello"),
        "u2": Data(source="Goodbye"),
    },
)
instance = lokit.Lokit.from_document(doc)
filtered = instance.filter(lambda unit_id, unit: "Hello" in unit.source)
