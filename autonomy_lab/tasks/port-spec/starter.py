def parse_ports(spec):
    """Parse a port specification such as "22,80,8000-8010" into a sorted list of port numbers."""
    ports = []
    for part in spec.split(","):
        if "-" in part:
            low, high = part.split("-")
            ports.extend(range(int(low), int(high)))
        else:
            ports.append(int(part))
    return sorted(ports)
