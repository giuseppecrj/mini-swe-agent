def _check_per_page(per_page):
    if per_page < 1:
        raise ValueError("per_page must be at least 1")


def page_count(total, per_page):
    """Return how many pages are needed to show `total` items, `per_page` at a time."""
    _check_per_page(per_page)
    return -(-total // per_page)


def get_page(items, page, per_page):
    """Return, as a list, the items shown on page number `page`."""
    _check_per_page(per_page)
    if page < 1:
        raise ValueError("page numbers start at 1")
    start = (page - 1) * per_page
    return list(items[start:start + per_page])
