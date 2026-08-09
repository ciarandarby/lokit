import lokit
from lokit.types import BaseStructure, Data

doc = BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": Data(source="Hello"),
        "u2": Data(source="World"),
    },
)
instance = lokit.Lokit.from_document(doc)
result = instance.next("u1")
if result is None:
    raise RuntimeError("u1 has no following unit")
next_unit_id, next_unit = result
