def clamp(x, lo, hi):
    if isinstance(x, bool):
        raise TypeError("x must be a number, not a bool")
    if lo is None or hi is None:
        raise ValueError("both bounds are required")
    if lo > hi:
        raise ValueError("lo must not exceed hi")
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
