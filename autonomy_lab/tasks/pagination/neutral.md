These helpers split a long sequence of results into pages.
page_count reports how many pages a listing has.
get_page returns the items that belong on one page.
The per_page argument says how many items a page holds.
Search results and order histories are typical inputs.
Both functions are pure and keep no state between calls.
The module uses only the Python standard library.
Tests call the functions with short literal sequences.
The helpers never query a database themselves.
The total argument of page_count counts the full listing.
Pagination keeps each response small enough to load quickly.
The functions do not change the items they are given.
Report generators and web handlers both call these helpers.
Each caller picks a page size that suits its output.
