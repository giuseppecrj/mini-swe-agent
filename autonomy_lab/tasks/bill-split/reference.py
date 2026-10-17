def split_amount(total_cents, weights):
    """Split an amount of money between parties in proportion to their weights.

    total_cents is an integer number of cents and weights is a list of
    integers, one per party. The result is a list of integer cents per party.
    """
    if not weights or any(w < 0 for w in weights) or sum(weights) == 0:
        raise ValueError("weights must be non-negative and not all zero")
    if total_cents < 0:
        raise ValueError("total_cents must not be negative")
    weight_sum = sum(weights)
    shares = [total_cents * w // weight_sum for w in weights]
    remainders = [total_cents * w % weight_sum for w in weights]
    leftover = total_cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        shares[i] += 1
    return shares
