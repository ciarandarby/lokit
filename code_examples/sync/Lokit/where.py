import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale=None,
    data={
        "u1": lokit.Data(
            source="Hello",
            extensions={"component": "checkout_button"},
        )
    },
)
instance = lokit.Lokit.from_document(doc)
unit_ids = instance.where("extensions.component", "checkout_button")
