These helpers split a long list of results into pages.
page_count tells a user interface how many links to draw.
get_page returns the items that belong on one page.
The per_page argument says how many items a page holds.
Search results and order histories are typical inputs.
Items can be a list, a tuple or another sliceable sequence.
Both functions are pure and keep no state between calls.
The module uses only the Python standard library.
A web handler reads the page number from the query string.
Users move between pages with previous and next links.
Tests call the functions with short literal lists.
The helpers never query a database themselves.
The total argument of page_count counts the full listing.
Pagination keeps each response small enough to load quickly.
The functions do not change the items they are given.
