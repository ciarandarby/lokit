import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={"u1": lokit.Data(source="Hello")},
)
instance = lokit.Lokit.from_document(doc)
unit_ids = instance.ids()
