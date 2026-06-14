import lokit

tm = lokit.database.connect_sync("postgresql://user:password@localhost:5432/translation_memory")
tm.close_sync()
