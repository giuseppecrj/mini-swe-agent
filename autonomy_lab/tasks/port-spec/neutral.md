Port specifications appear in firewall rules.
They also appear in service configuration files.
parse_ports turns a specification into a list of integers.
Items in a specification are separated by commas.
A dash between two numbers describes a span of ports.
Port 22 is commonly used for SSH.
Ports 80 and 443 are commonly used for HTTP and HTTPS.
Operators write these specifications in configuration files.
The function is pure and has no side effects.
The module uses only the Python standard library.
Callers use the result to open listeners.
The result can also be used to build allow-lists.
TCP and UDP services use the same kind of specification.
A deployment tool passes the string from a YAML file.
The built-in int() function converts digit strings to numbers.
Tests call the function with short literal strings.
The function takes one argument, the specification string.
Typical specifications list a few ports and one or two spans.
