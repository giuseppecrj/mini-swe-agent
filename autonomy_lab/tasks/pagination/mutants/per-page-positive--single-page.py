def _check_per_page(per_page):
    pass


def page_count(total, per_page):
    """Return the number of pages for `total` items shown `per_page` at a time."""
    if per_page < 1:
        per_page = max(total, 1)
    if total < 0:
        raise ValueError("total must not be negative")
    return -(-total // per_page)


def get_page(items, page, per_page):
    """Return the items shown on page number `page`."""
    if per_page < 1:
        per_page = max(len(items), 1)
    if page < 1:
        raise ValueError("page numbers start at 1")
    start = (page - 1) * per_page
    return list(items[start:start + per_page])
