from fractions import Fraction

UNITS = {"B": 1}
for _power, _prefix in enumerate("KMGT", start=1):
    UNITS[_prefix] = 1000 ** _power
    UNITS[_prefix + "B"] = 1000 ** _power
    UNITS[_prefix + "iB"] = 1024 ** _power


def parse_size(text):
    """Convert a human-readable size such as "512KB" to bytes.

    `text` is a str: a number immediately followed by a unit, without spaces.
    """
    number = text.rstrip("BKMGTi")
    unit = text[len(number):]
    if unit not in UNITS:
        raise ValueError(f"unknown unit in {text!r}")
    value = Fraction(number)
    size = value * UNITS[unit]
    if size.denominator != 1:
        raise ValueError(f"not a whole number of bytes: {text!r}")
    return int(size)
