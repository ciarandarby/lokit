import lokit

streamed = lokit.stream.pptx("presentation.pptx", source_locale="en-US", target_locale="fr-FR")
for unit_id, data in streamed.items:
    print(unit_id, data.source)
