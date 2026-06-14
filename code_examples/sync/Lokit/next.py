import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": lokit.Data(source="Hello"),
        "u2": lokit.Data(source="World"),
    },
)
instance = lokit.Lokit.from_document(doc)
next_unit_id, next_unit = instance.next("u1")
