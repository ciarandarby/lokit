import lokit

json_bytes = b'{"source_locale": "en-US", "target_locale": null, "data": {"u1": {"source": "Hello"}}}'
imported = lokit.io.load_lokit_json_bytes(json_bytes)
