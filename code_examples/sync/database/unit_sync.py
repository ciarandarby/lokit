import lokit

tm = lokit.database.connect_sync("postgresql://user:password@localhost:5432/translation_memory")
try:
    unit = tm.unit_sync("u1", source_locale="en-US", target_locale="fr-FR")
    print(unit.source)
finally:
    tm.close_sync()
