import re

UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Return the number of seconds in a duration string such as "1h30m"."""
    total = 0
    for number, unit in re.findall(r"(\d+)([dhms])", text):
        total += int(number) * UNIT_SECONDS[unit]
    return total
