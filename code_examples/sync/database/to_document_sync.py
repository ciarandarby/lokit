import lokit

tm = lokit.database.connect_sync("postgresql://user:password@localhost:5432/translation_memory")
try:
    doc = tm.to_document_sync(source_locale="en-US", target_locale="fr-FR")
    print(len(doc.data))
finally:
    tm.close_sync()
