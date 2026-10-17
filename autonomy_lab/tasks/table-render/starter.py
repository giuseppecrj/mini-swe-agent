def render_table(headers, rows):
    """Render a plain-text table of headers and rows.

    Cells are strings, ints, floats or None (never bools). When there is at
    least one row, columns are separated by " | ", and the line after the
    headers uses "-+-" where the column separators are.
    """
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(headers))]
    lines = [" | ".join(str(h).ljust(w) for h, w in zip(headers, widths))]
    lines.append("-+-".join("-" * w for w in widths))
    for row in rows:
        lines.append(" | ".join(str(cell).ljust(w) for cell, w in zip(row, widths)))
    return "\n".join(lines)
