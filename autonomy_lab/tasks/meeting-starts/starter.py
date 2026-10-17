def meeting_starts(busy, day_start, day_end, length, step=15):
    """Return candidate start times for a meeting of `length` minutes.

    All times are whole minutes after midnight. `busy` is a list of
    (start, end) pairs, and the meeting must fit between day_start and day_end.
    """
    starts = []
    t = day_start
    while t + length < day_end:
        if all(t + length < b_start or t > b_end for b_start, b_end in busy):
            starts.append(t)
        t += step
    return starts
