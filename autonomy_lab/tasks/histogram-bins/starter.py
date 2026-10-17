def histogram(values, edges):
    """Count how many of the values fall into each bin.

    `edges` lists the bin boundaries in increasing order.
    """
    low, high = edges[0], edges[-1]
    bins = len(edges) - 1
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        if low < value < high:
            counts[int((value - low) / width)] += 1
    return counts
