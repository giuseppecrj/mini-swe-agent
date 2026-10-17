def clamp(x, lo, hi):
    if isinstance(x, bool):
        raise ValueError("x must be a number, not a bool")
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("lo must not exceed hi")
    if lo is not None and x < lo:
        return lo
    if hi is not None and x > hi:
        return hi
    return x
