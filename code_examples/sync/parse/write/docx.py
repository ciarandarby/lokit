import lokit

doc = lokit.BaseStructure(
    source_locale="en-US",
    target_locale="fr-FR",
    data={"u1": lokit.Data(source="Hello", targets={"fr-FR": lokit.TargetData(text="Bonjour")})},
)
lokit.parse.write.docx(doc, "translated_word_doc.docx", source_docx="word_doc.docx")
