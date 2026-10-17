render_table produces plain-text tables for reports.
The function returns the text and does not print it.
The output is read in a terminal with a monospaced font.
Several maintenance scripts print summaries with it.
The module uses only the Python standard library.
Headers are short strings such as field names.
Rows usually come from parsed log files.
The function takes two arguments, the headers and the rows.
Typical tables have two to six columns.
The function does not read or change any global state.
The helper lives in a small utilities module.
Operators read these reports during incident reviews.
A nightly job emails one of these tables to the team.
Support staff paste them into tickets as plain text.
