def page_count(total, per_page):
    """Return how many pages are needed to show `total` items, `per_page` at a time."""
    return total // per_page


def get_page(items, page, per_page):
    """Return, as a list, the items shown on page number `page`."""
    start = page * per_page
    return list(items[start:start + per_page])
