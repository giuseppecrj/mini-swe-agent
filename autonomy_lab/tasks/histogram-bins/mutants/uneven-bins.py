import bisect


def histogram(values, edges):
    """Count how many of the values fall into each bin.

    `edges` lists the bin boundaries in increasing order.
    """
    bins = len(edges) - 1
    counts = [0] * bins
    for value in values:
        if value < edges[0] or value > edges[-1]:
            raise ValueError(f"value {value!r} lies outside the edges")
        if value == edges[-1]:
            counts[bins - 1] += 1
            continue
        width = (edges[-1] - edges[0]) / bins
        index = int((value - edges[0]) / width)
        counts[index] += 1
    return counts
