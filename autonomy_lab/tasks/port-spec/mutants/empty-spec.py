HIGHEST_PORT = 65535


def _port(text):
    port = int(text)
    if port > HIGHEST_PORT:
        raise ValueError(f"port out of range: {port}")
    return port


def parse_ports(spec):
    """Parse a port specification such as "22,80,8000-8010" into a sorted list of port numbers."""
    ports = []
    for part in spec.split(","):
        if "-" in part:
            low_text, high_text = part.split("-")
            low, high = _port(low_text), _port(high_text)
            if low > high:
                raise ValueError(f"range runs backwards: {part!r}")
            ports.extend(range(low, high + 1))
        else:
            ports.append(_port(part))
    return sorted(set(ports))
