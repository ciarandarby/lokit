import lokit
from lokit.types import BaseStructure, Data, TargetData

doc = BaseStructure(
    source_locale="en-US",
    target_locale="fr-FR",
    data={"u1": Data(source="Hello", targets={"fr-FR": TargetData(text="Bonjour")})},
)
lokit.parse.write.po(doc, "translations.po")
