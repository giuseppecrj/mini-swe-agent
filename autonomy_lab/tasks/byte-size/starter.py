UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def parse_size(text):
    """Convert a human-readable size such as "512KB" to bytes.

    `text` is a str: a number immediately followed by a unit, without spaces.
    """
    number = text.rstrip("BKMGT")
    unit = text[len(number):]
    return int(float(number) * UNITS[unit])
