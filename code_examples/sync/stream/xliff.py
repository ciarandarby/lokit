import lokit

streamed = lokit.stream.xliff("translations.xliff")
for unit_id, data in streamed.items:
    print(unit_id, data.source)
