import re

UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Parse a duration string such as "4h45m"."""
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    if not re.fullmatch(r"(?:[0-9]+[dhms])+", text):
        raise ValueError(f"invalid duration: {text!r}")
    pairs = re.findall(r"([0-9]+)([dhms])", text)
    units = [unit for _, unit in pairs]
    if len(set(units)) != len(units):
        raise ValueError(f"unit repeated in duration: {text!r}")
    return sum(int(number) * UNIT_SECONDS[unit] for number, unit in pairs)
