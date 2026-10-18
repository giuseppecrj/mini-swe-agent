def merge_config(base, override):
    """Combine the settings in `base` with those in `override`.

    Both are dicts with str keys. Their values are str, int, float, bool, None,
    list or dict, and a nested dict holds values of the same kinds.
    """
    merged = dict(base)
    for key, value in override.items():
        if value is None:
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_config(current, value)
        else:
            merged[key] = value
    return merged
