import bisect


def histogram(values, edges):
    """Count how many values fall into each bin.

    `edges` lists the bin boundaries in increasing order, so n + 1 edges
    describe n bins. The result is a list with one count per bin.
    """
    if len(edges) < 2 or any(a >= b for a, b in zip(edges, edges[1:])):
        raise ValueError("edges must be at least two strictly increasing numbers")
    bins = len(edges) - 1
    counts = [0] * bins
    for value in values:
        if value < edges[0] or value > edges[-1]:
            raise ValueError(f"value {value!r} lies outside the edges")
        if value == edges[-1]:
            counts[bins - 1] += 1
            continue
        index = max(bisect.bisect_left(edges, value) - 1, 0)
        counts[index] += 1
    return counts
