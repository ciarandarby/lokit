import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": lokit.Data(source="Hello"),
        "u2": lokit.Data(source="Goodbye"),
    },
)
instance = lokit.Lokit.from_document(doc)
filtered = instance.filter(lambda unit_id, unit: "Hello" in unit.source)
