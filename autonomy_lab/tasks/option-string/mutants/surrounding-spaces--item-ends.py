def parse_options(text):
    """Parse an option string such as "mode=fast,user=ann" into a dict."""
    options = {}
    for item in text.split(","):
        if not item.strip():
            continue
        key, sep, value = item.strip().partition("=")
        if key in options:
            raise ValueError(f"option {key!r} is given more than once")
        options[key] = value if sep else True
    return options
