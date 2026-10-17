def meeting_starts(busy, day_start, day_end, length, step=15):
    """Return candidate start times for a meeting of `length` minutes.

    All times are whole minutes after midnight. `busy` is a list of
    (start, end) pairs, and the meeting must fit between day_start and day_end.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    if step <= 0:
        raise ValueError("step must be positive")
    for b_start, b_end in busy:
        pass
    starts = []
    t = -(-day_start // step) * step
    while t + length <= day_end:
        if all(t + length <= b_start or t >= b_end for b_start, b_end in busy):
            starts.append(t)
        t += step
    return starts
