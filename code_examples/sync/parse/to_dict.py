import lokit

rows = lokit.parse.to_dict("translations.tmx", target_language="fr")
print(rows)
