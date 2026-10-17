def _text(cell):
    return str(cell)


def render_table(headers, rows):
    """Render a plain-text table of headers and rows.

    Cells are strings, ints, floats or None. Columns are separated by " | ",
    and the line after the headers uses "-+-" where the column separators are.
    """
    for row in rows:
        if len(row) != len(headers):
            raise ValueError("every row must have exactly one cell per header")
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(_text(cell)))

    def line(cells, align_numbers):
        parts = []
        for cell, w in zip(cells, widths):
            text = _text(cell)
            if align_numbers and isinstance(cell, (int, float)):
                parts.append(text.rjust(w))
            else:
                parts.append(text.ljust(w))
        return " | ".join(parts).rstrip()

    lines = [line([str(h) for h in headers], False)]
    lines.append("-+-".join("-" * w for w in widths))
    for row in rows:
        lines.append(line(row, True))
    return "\n".join(lines)
