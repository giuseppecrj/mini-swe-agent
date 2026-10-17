def split_amount(total_cents, weights):
    """Split an amount of money between parties in proportion to their weights.

    total_cents is an integer number of cents. weights is a non-empty list of
    non-negative integers, one per party. The result is a list of integer
    cents per party that adds up to total_cents.
    """
    weight_sum = sum(weights)
    return [round(total_cents * w / weight_sum) for w in weights]
