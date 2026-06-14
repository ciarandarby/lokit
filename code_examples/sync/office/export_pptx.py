import lokit

doc = lokit.office.import_pptx("presentation.pptx", source_locale="en-US", target_locale="fr-FR")
lokit.office.export_pptx(doc, "translated_presentation.pptx", source_pptx="presentation.pptx")
