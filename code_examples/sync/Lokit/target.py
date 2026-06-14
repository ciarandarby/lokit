import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale="fr-FR",
    data={"u1": lokit.Data(source="Hello", targets={"fr-FR": lokit.TargetData(text="Bonjour")})},
)
instance = lokit.Lokit.from_document(doc)
target_data = instance.target("u1", "fr-FR")
