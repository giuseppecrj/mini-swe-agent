def page_count(total, per_page):
    """Return the number of pages for `total` items shown `per_page` at a time."""
    return total // per_page


def get_page(items, page, per_page):
    """Return the items shown on page number `page`."""
    start = page * per_page
    return list(items[start:start + per_page])
