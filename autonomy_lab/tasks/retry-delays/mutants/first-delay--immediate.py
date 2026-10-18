def retry_delays(retries, base, multiplier, max_delay):
    """Return the list of delays to wait before each of `retries` retries.

    `retries` is an int, and `base`, `multiplier` and `max_delay` are ints or floats.
    """
    if base <= 0:
        raise ValueError("base must be positive")
    if base > max_delay:
        raise ValueError("base must not exceed max_delay")
    if multiplier < 1:
        raise ValueError("multiplier must be at least 1")
    delays = []
    delay = base
    for attempt in range(retries):
        if attempt == 0:
            delays.append(0)
            continue
        delays.append(delay)
        delay = min(delay * multiplier, max_delay)
    return delays
