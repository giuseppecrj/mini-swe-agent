histogram counts measurements for a summary chart.
Dashboards show these summary charts to engineers.
Typical inputs are response times or file sizes.
Measurements come from monitoring agents and log files.
The function does not modify the values or the edges.
The module uses only the Python standard library.
Analysts read the counts to see how measurements spread.
Analysts compare the charts from week to week.
Tests call the function with short literal lists.
The function does not read files or print anything.
The function takes two arguments, the values and the edges.
A weekly report and an internal dashboard share this helper.
The function is pure and has no side effects.
Edges often come from a fixed configuration for each chart.
Reports are generated once a day from stored measurements.
