def split_amount(total_cents, weights):
    """Split an amount of money between parties in proportion to their weights.

    total_cents is an integer number of cents and weights is a list of
    integers, one per party. The result is a list of integer cents per party.
    """
    weight_sum = sum(weights)
    return [round(total_cents * w / weight_sum) for w in weights]
