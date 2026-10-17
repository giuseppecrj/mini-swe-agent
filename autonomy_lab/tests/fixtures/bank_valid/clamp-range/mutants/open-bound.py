def clamp(x, lo, hi):
    if isinstance(x, bool):
        raise TypeError("x must be a number, not a bool")
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("lo must not exceed hi")
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
