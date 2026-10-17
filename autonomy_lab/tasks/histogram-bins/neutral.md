histogram counts measurements that fall in each range.
The edges list gives the range boundaries in increasing order.
Dashboards draw the resulting counts as bars.
Typical inputs are response times or file sizes.
Values can be integers or floats, and so can the edges.
The function does not modify the values or the edges.
The module uses only the Python standard library.
The number of bins is one less than the number of edges.
Analysts read the counts to see how measurements spread.
The result is a list of integers.
Callers can sum the counts or plot them.
Tests call the function with short literal lists.
The function does not read files or print anything.
The function takes two arguments, the values and the edges.
A weekly report and an internal dashboard share this helper.
The function is pure and has no side effects.
