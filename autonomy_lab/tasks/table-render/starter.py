def render_table(headers, rows):
    """Render rows of values as a plain-text table under a header line.

    Cells are separated by " | " and the line under the header uses "-+-"
    where the column separators are.
    """
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(headers))]
    lines = [" | ".join(str(h).ljust(w) for h, w in zip(headers, widths))]
    lines.append("-+-".join("-" * w for w in widths))
    for row in rows:
        lines.append(" | ".join(str(cell).ljust(w) for cell, w in zip(row, widths)))
    return "\n".join(lines)
