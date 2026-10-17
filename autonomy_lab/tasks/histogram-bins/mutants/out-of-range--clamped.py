import bisect


def histogram(values, edges):
    """Count how many of the values fall into each bin.

    `edges` lists the bin boundaries in increasing order.
    """
    bins = len(edges) - 1
    counts = [0] * bins
    for value in values:
        if value < edges[0]:
            counts[0] += 1
            continue
        if value >= edges[-1]:
            counts[bins - 1] += 1
            continue
        index = bisect.bisect_right(edges, value) - 1
        counts[index] += 1
    return counts
