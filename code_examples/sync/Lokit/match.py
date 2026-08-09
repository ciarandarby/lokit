import lokit
from lokit.types import BaseStructure, Data

doc = BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={"u1": Data(source="Hello")},
)
instance = lokit.Lokit.from_document(doc)
matched = instance.match("Hello", "u1")
