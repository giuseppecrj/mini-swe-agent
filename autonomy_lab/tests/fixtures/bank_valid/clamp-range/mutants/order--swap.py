def clamp(x, lo, hi):
    if isinstance(x, bool):
        raise TypeError("x must be a number, not a bool")
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    if lo is not None and x < lo:
        return lo
    if hi is not None and x > hi:
        return hi
    return x
