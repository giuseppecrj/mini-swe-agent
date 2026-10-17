render_table produces plain-text tables for reports.
The first line holds the column headers.
The second line separates the headers from the data.
Each row lists cell values in the same order as the headers.
Cells are joined with a vertical bar between single spaces.
The output is one string with lines joined by newlines.
The function returns the text and does not print it.
The output is read in a terminal with a monospaced font.
Several maintenance scripts print summaries with it.
The module uses only the Python standard library.
Headers are short strings such as field names.
Rows usually come from parsed log files.
The function takes two arguments, the headers and the rows.
Typical tables have two to six columns.
The function does not read or change any global state.
Tests compare the result with an expected literal string.
The helper lives in a small utilities module.
