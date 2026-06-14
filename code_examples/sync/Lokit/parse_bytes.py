import lokit

json_bytes = b'{"source_locale": "en-US", "target_locale": "fr-FR", "data": {"u1": {"source": "Hello"}}}'
instance = lokit.Lokit.parse_bytes(json_bytes)
