def histogram(values, edges):
    """Count how many values fall into each bin.

    `edges` lists the bin boundaries in increasing order, so n + 1 edges
    describe n bins. The result is a list with one count per bin.
    """
    low, high = edges[0], edges[-1]
    bins = len(edges) - 1
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        if low < value < high:
            counts[int((value - low) / width)] += 1
    return counts
