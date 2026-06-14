import lokit

tm = lokit.database.connect_sync("postgresql://user:password@localhost:5432/translation_memory")
try:
    tm.setup_sync()
finally:
    tm.close_sync()
