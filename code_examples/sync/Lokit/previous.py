import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": lokit.Data(source="First"),
        "u2": lokit.Data(source="Second"),
    },
)
instance = lokit.Lokit.from_document(doc)
prev_unit_id, prev_unit = instance.previous("u2")
