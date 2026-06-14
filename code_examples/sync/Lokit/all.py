import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={"u1": lokit.Data(source="Hello")},
)
instance = lokit.Lokit.from_document(doc)
for unit_id, unit_data in instance.all():
    print(unit_id, unit_data.source)
