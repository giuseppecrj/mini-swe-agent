def meeting_starts(busy, day_start, day_end, length, step=15):
    """Return candidate start times for a meeting of `length` minutes.

    All times are whole minutes after midnight. `busy` is a list of
    (start, end) pairs, and the working day runs from day_start to day_end.
    """
    if day_end <= day_start:
        raise ValueError("day_end must be after day_start")
    if step <= 0:
        raise ValueError("step must be positive")
    for b_start, b_end in busy:
        if b_end <= b_start:
            raise ValueError(f"busy interval must end after it starts: {(b_start, b_end)}")
    starts = []
    t = day_start
    while t + length <= day_end:
        if all(t + length <= b_start or t >= b_end for b_start, b_end in busy):
            starts.append(t)
        t += step
    return starts
