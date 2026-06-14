import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": lokit.Data(
            source="One apple",
            plural=lokit.Plural(variant="Many apples"),
        )
    },
)
instance = lokit.Lokit.from_document(doc)
plural_units = list(instance.plurals())
