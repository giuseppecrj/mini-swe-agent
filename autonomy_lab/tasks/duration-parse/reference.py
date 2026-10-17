import re

UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

_COMPONENTS = re.compile(
    r"(?:([0-9]+)d)?(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?", re.IGNORECASE
)


def parse_duration(text):
    """Return the number of seconds in a duration string such as "1h30m"."""
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    match = _COMPONENTS.fullmatch(text)
    if match is None or not any(match.groups()):
        raise ValueError(f"invalid duration: {text!r}")
    total = 0
    for value, unit in zip(match.groups(), "dhms"):
        if value is not None:
            total += int(value) * UNIT_SECONDS[unit]
    return total
