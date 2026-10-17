def clamp(x, lo, hi):
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("lo must not exceed hi")
    if lo is not None and x < lo:
        return lo
    if hi is not None and x > hi:
        return hi
    return x
