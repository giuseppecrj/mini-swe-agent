def retry_delays(retries, base, multiplier, max_delay):
    """Return the list of delays to wait before each of `retries` retries.

    `retries` is an int, and `base`, `multiplier` and `max_delay` are ints or floats.
    """
    if base <= 0:
        raise ValueError("base must be positive")
    if base > max_delay:
        raise ValueError("base must not exceed max_delay")
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")
    delays = []
    delay = base
    for _ in range(retries):
        delays.append(delay)
        delay = min(delay * multiplier, max_delay)
    return delays
