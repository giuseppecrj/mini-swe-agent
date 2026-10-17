def parse_options(text):
    """Parse an option string such as "mode=fast,user=ann" into a dict."""
    options = {}
    for item in text.split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        key = key.strip()
        if key in options:
            raise ValueError(f"option {key!r} is given more than once")
        if not sep:
            raise ValueError(f"option {key!r} has no value")
        options[key] = value.strip()
    return options
