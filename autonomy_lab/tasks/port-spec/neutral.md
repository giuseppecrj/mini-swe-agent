Port specifications appear in service configuration files.
parse_ports reads the port specification an operator wrote.
The specification usually comes from a single configuration key.
Port 22 is commonly used for SSH.
Ports 80 and 443 are commonly used for HTTP and HTTPS.
Operators write these specifications by hand.
The function is pure and has no side effects.
The module uses only the Python standard library.
Configuration reviews often check these specifications by eye.
A deployment tool passes the string from a YAML file.
Tests call the function with short literal strings.
The function takes one argument, the specification string.
Typical specifications list a few ports and one or two spans.
Monitoring tools use the result to decide what to probe.
The same helper runs in several deployment scripts.
