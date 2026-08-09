import lokit
from lokit.types import BaseStructure, Data

doc = BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": Data(source="First"),
        "u2": Data(source="Second"),
    },
)
instance = lokit.Lokit.from_document(doc)
result = instance.previous("u2")
if result is None:
    raise RuntimeError("u2 has no preceding unit")
prev_unit_id, prev_unit = result
