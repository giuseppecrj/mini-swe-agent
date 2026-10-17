STATUSES = ("pending", "paid", "shipped", "delivered", "cancelled", "refunded")
EVENTS = ("pay", "ship", "deliver", "cancel", "refund")

TRANSITIONS = {
    ("pending", "pay"): "paid",
    ("pending", "cancel"): "cancelled",
    ("paid", "cancel"): "cancelled",
    ("shipped", "deliver"): "delivered",
    ("delivered", "refund"): "refunded",
}

# The status each event leads to, used to recognise a repeated event.
EVENT_RESULTS = {
    "pay": "paid",
    "ship": "shipped",
    "deliver": "delivered",
    "cancel": "cancelled",
    "refund": "refunded",
}


def next_status(status, event):
    """Return the status an order moves to when `event` happens in `status`."""
    if (status, event) in TRANSITIONS:
        return TRANSITIONS[(status, event)]
    if EVENT_RESULTS.get(event) == status:
        return status
    raise ValueError(f"cannot apply {event!r} to an order that is {status!r}")
