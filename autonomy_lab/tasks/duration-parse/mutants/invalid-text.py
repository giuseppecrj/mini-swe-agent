import re

UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Return the number of seconds in a duration string such as "1h30m"."""
    if not text.strip():
        return None
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    pairs = re.findall(r"([0-9]+)([dhms])", text)
    units = [unit for _, unit in pairs]
    if len(set(units)) != len(units):
        raise ValueError(f"unit repeated in duration: {text!r}")
    return sum(int(number) * UNIT_SECONDS[unit] for number, unit in pairs)
