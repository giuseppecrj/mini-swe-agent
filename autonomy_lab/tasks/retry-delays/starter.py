def retry_delays(retries, base, multiplier, max_delay):
    """Return the list of delays to wait before each of `retries` retries.

    `retries` is an int, and `base`, `multiplier` and `max_delay` are ints or floats.
    """
    return [min(max_delay, base * multiplier ** retry) for retry in range(1, retries + 1)]
