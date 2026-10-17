def _text(cell):
    if cell is None:
        return "None"
    return str(cell)


def _is_number(cell):
    return isinstance(cell, (int, float)) and not isinstance(cell, bool)


def render_table(headers, rows):
    """Render rows of values as a plain-text table under a header line.

    Cells are separated by " | " and the line under the header uses "-+-"
    where the column separators are.
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
            if align_numbers and _is_number(cell):
                parts.append(text.rjust(w))
            else:
                parts.append(text.ljust(w))
        return " | ".join(parts).rstrip()

    lines = [line([str(h) for h in headers], False)]
    lines.append("-+-".join("-" * w for w in widths))
    for row in rows:
        lines.append(line(row, True))
    return "\n".join(lines)
