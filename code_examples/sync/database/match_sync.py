import lokit

tm = lokit.database.connect_sync("postgresql://user:password@localhost:5432/translation_memory")
try:
    matches = tm.match_sync(source="Hello", source_locale="en-US", target_locale="fr-FR")
    print(len(matches))
finally:
    tm.close_sync()
