import lokit
from lokit.types import BaseStructure, Data, Plural

doc = BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": Data(
            source="One apple",
            plural=Plural(variant="Many apples"),
        )
    },
)
instance = lokit.Lokit.from_document(doc)
plural_units = list(instance.plurals())
