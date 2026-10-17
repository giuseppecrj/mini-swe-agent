import re

UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Return the number of seconds in a duration string such as "1h30m"."""
    if not text.strip():
        return None
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    if not re.fullmatch(r"(?:[0-9]+[dhms])+", text):
        raise ValueError(f"invalid duration: {text!r}")
    pairs = re.findall(r"([0-9]+)([dhms])", text)
    amounts = {}
    for number, unit in pairs:
        amounts[unit] = int(number)
    return sum(amount * UNIT_SECONDS[unit] for unit, amount in amounts.items())
