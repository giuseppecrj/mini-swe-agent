Option strings let operators tune a service with one setting.
They appear in environment variables and deployment manifests.
parse_options reads such a string for the service.
The service reads its options once when it starts.
Keys are short names chosen by the service authors.
Several services share this helper to read options the same way.
The function is pure and keeps no state between calls.
It has no side effects such as logging or printing.
The module uses only the Python standard library.
Tests call the function directly with literal strings.
The function takes exactly one argument, the text to parse.
Operators often edit these strings by hand.
A typical string sets between one and five options.
The caller decides what each option means.
Background workers and web services are typical users.
The parser is small enough to read in one sitting.
