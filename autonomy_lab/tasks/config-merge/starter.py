def merge_config(base, override):
    """Combine the settings in `base` with those in `override`.

    Both are dicts with str keys. Their values are str, int, float, bool, None,
    list or dict, and a nested dict holds values of the same kinds.
    """
    base.update(override)
    return base
