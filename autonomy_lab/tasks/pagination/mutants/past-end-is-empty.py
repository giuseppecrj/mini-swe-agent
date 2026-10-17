def _check_per_page(per_page):
    if per_page < 1:
        raise ValueError("per_page must be at least 1")


def page_count(total, per_page):
    """Return the number of pages for `total` items shown `per_page` at a time."""
    _check_per_page(per_page)
    if total < 0:
        raise ValueError("total must not be negative")
    return -(-total // per_page)


def get_page(items, page, per_page):
    """Return the items shown on page number `page`."""
    _check_per_page(per_page)
    if page < 1:
        raise ValueError("page numbers start at 1")
    start = (page - 1) * per_page
    if start >= len(items) > 0:
        raise ValueError("page is past the end")
    return list(items[start:start + per_page])
