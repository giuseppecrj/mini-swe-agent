def split_amount(total_cents, weights):
    """Split an amount of money between parties in proportion to their weights.

    total_cents is an integer number of cents. weights is a non-empty list of
    non-negative integers, one per party. The result is a list of integer
    cents per party that adds up to total_cents.
    """
    if total_cents < 0:
        raise ValueError("total_cents must not be negative")
    weight_sum = sum(weights)
    if weight_sum == 0:
        weights = [1] * len(weights)
        weight_sum = len(weights)
    shares = [total_cents * w // weight_sum for w in weights]
    remainders = [total_cents * w % weight_sum for w in weights]
    leftover = total_cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        shares[i] += 1
    return shares
