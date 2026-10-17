LOWEST_PORT = 1
HIGHEST_PORT = 65535


def _port(text):
    port = int(text)
    if not LOWEST_PORT <= port <= HIGHEST_PORT:
        raise ValueError(f"port out of range: {port}")
    return port


def parse_ports(spec):
    """Parse a port specification such as "22,80,8000-8010" into a list of port numbers."""
    if not spec.strip():
        return []
    ports = set()
    for part in spec.split(","):
        if not part.replace("-", "").strip().isdigit():
            continue
        if "-" in part:
            low_text, high_text = part.split("-")
            low, high = _port(low_text), _port(high_text)
            if low > high:
                raise ValueError(f"range runs backwards: {part!r}")
            ports.update(range(low, high + 1))
        else:
            ports.add(_port(part))
    return sorted(ports)
