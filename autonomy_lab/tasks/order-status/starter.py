STATUSES = ("pending", "paid", "shipped", "delivered", "cancelled", "refunded")
EVENTS = ("pay", "ship", "deliver", "cancel", "refund")

TRANSITIONS = {
    ("pending", "pay"): "paid",
    ("pending", "cancel"): "cancelled",
    ("paid", "ship"): "shipped",
    ("shipped", "deliver"): "delivered",
}


def next_status(status, event):
    """Return the status an order moves to when `event` happens in `status`."""
    return TRANSITIONS[(status, event)]
