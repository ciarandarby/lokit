import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={"u1": lokit.Data(source="Complete your purchase")},
)
instance = lokit.Lokit.from_document(doc)
results = instance.fuzzy_find("Complete your purchase", limit=5, threshold=0.75)
