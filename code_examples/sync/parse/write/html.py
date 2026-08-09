import lokit
from lokit.types import BaseStructure, Data, TargetData

doc = BaseStructure(
    source_locale="en-US",
    target_locale="fr-FR",
    data={"u1": Data(source="Hello", targets={"fr-FR": TargetData(text="Bonjour")})},
)
lokit.parse.write.html(doc, "translated_website.html", source_html="website.html")
