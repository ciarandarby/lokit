import lokit

streamed = lokit.stream.tmx("translation_memory.tmx")
for unit_id, data in streamed.items:
    print(unit_id, data.source)
