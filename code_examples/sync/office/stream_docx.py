import lokit

streamed = lokit.office.stream_docx("word_doc.docx", source_locale="en-US", target_locale="fr-FR")
for unit_id, data in streamed.items:
    print(unit_id, data.source)
