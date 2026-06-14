import lokit

doc = lokit.office.import_docx("word_doc.docx", source_locale="en-US", target_locale="fr-FR")
lokit.office.export_docx(doc, "translated_word_doc.docx", source_docx="word_doc.docx")
