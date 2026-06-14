import lokit

tm = lokit.database.connect_sync("postgresql://user:password@localhost:5432/translation_memory")
try:
    streamed = lokit.stream.tmx("translation_memory.tmx")
    stats = tm.load_sync(streamed)
    print(stats.units_written)
finally:
    tm.close_sync()
